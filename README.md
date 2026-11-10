# LLM inference on Amazon EKS GPUs behind Envoy Gateway

**Step-by-step guide with diagrams, real results and screenshots:** https://patel0ankur.github.io/eks-gpu-llm-inference/

Serve an open model ([Qwen2.5-0.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct)) with **vLLM** on NVIDIA A10G GPUs in Amazon EKS. A small FastAPI app sits in front, and **Envoy Gateway** (Kubernetes Gateway API) handles routing, rate limiting, retries and least-request load balancing behind an AWS Network Load Balancer.

```text
Client (one allowed IP)
  → NLB (L4, internet-facing, source-restricted)
  → Envoy public-gw (L7: /v1/ask + /healthz only, 20 req/s per Envoy Pod, retry once on 503)
  → chat-api ×2 (FastAPI on CPU nodes, 32 in-flight per Pod → 503 + Retry-After)
  → Envoy llm-gw (ClusterIP: least-request, outlier detection, circuit breaker)
  → vLLM ×2 (one per g5.xlarge GPU node, OpenAI-compatible API, API key required)
```

All AWS infrastructure and IAM is CloudFormation. Kubernetes objects are plain manifests plus three Helm charts.

## Repository layout

| Path | What |
| --- | --- |
| [`cfn/eks-gpu-cluster.yaml`](cfn/eks-gpu-cluster.yaml) | VPC (3 AZs, 1 NAT), EKS cluster (KMS secrets encryption, control-plane logs), system node group (m6i.large) and GPU node group (g5.xlarge, AL2023 NVIDIA, tainted). Details: [`cfn/README.md`](cfn/README.md) |
| [`cfn/eks-addons-iam.yaml`](cfn/eks-addons-iam.yaml) | IAM policy and role for the AWS Load Balancer Controller via **EKS Pod Identity** |
| [`chat-app/`](chat-app/) | FastAPI app tier: OpenAI client to vLLM, backpressure, `/healthz` and `/readyz` |
| [`k8s/gpu/01-llm-vllm.yaml`](k8s/gpu/01-llm-vllm.yaml) | vLLM Deployment (2 replicas, 1 GPU each) and ClusterIP Service |
| [`k8s/gpu/02-chat-app.yaml`](k8s/gpu/02-chat-app.yaml) | chat-api Deployment, Service and PodDisruptionBudget (image set by `envsubst`) |
| [`k8s/gateway/01-gateways.yaml`](k8s/gateway/01-gateways.yaml) | GatewayClass, EnvoyProxy configs, `public-gw` (NLB) and `llm-gw` (internal) (source CIDR set by `envsubst`) |
| [`k8s/gateway/02-routes.yaml`](k8s/gateway/02-routes.yaml) | HTTPRoutes and BackendTrafficPolicies |
| [`k8s/monitoring/01-dcgm-exporter.yaml`](k8s/monitoring/01-dcgm-exporter.yaml) | NVIDIA DCGM Exporter DaemonSet (GPU nodes only) and headless Service on `:9400` |
| [`k8s/monitoring/02-dcgm-servicemonitor.yaml`](k8s/monitoring/02-dcgm-servicemonitor.yaml) | ServiceMonitor: Prometheus scrapes each exporter every 15 s, keeping the GPU workload's Pod labels |
| [`k8s/monitoring/03-gpu-dashboard.yaml`](k8s/monitoring/03-gpu-dashboard.yaml) | Grafana GPU hardware dashboard (12 panels) as a ConfigMap, loaded by the Grafana sidecar |
| [`k8s/monitoring/04-gpu-alerts.yaml`](k8s/monitoring/04-gpu-alerts.yaml) | PrometheusRule: 6 GPU alerts (exporter down, temperature, memory, row-remap errors, idle GPU) |
| [`k8s/monitoring/05-vllm-podmonitor.yaml`](k8s/monitoring/05-vllm-podmonitor.yaml) | PodMonitor: Prometheus scrapes vLLM's queue, KV-cache, token and latency metrics |
| [`k8s/monitoring/06-vllm-dashboard.yaml`](k8s/monitoring/06-vllm-dashboard.yaml) | Grafana dashboard "Workload: vLLM on each GPU": queue, tokens, latency per Pod, joined with its GPU |
| [`k8s/monitoring/07-envoy-podmonitor.yaml`](k8s/monitoring/07-envoy-podmonitor.yaml) | PodMonitor: Prometheus scrapes both Envoy gateways (client latency, status codes, rate limiting) |
| [`k8s/monitoring/08-envoy-dashboard.yaml`](k8s/monitoring/08-envoy-dashboard.yaml) | Grafana dashboard "Gateways: Envoy": traffic, status codes, client latency, backend health, retries, ejections |
| [`k8s/monitoring/kube-prometheus-stack-values.yaml`](k8s/monitoring/kube-prometheus-stack-values.yaml) | Prometheus, Grafana and Alertmanager (kube-prometheus-stack 93.1.1) on the system nodes, ClusterIP only |
| [`scripts/bypass-url.sh`](scripts/bypass-url.sh) | `kubectl port-forward` to `public-gw` on 127.0.0.1, for testing without the NLB |

## Prerequisites

- An AWS account for **dev/test**, with a quota of at least 8 vCPUs for "Running On-Demand G and VT instances" (2 × g5.xlarge)
- `aws`, `kubectl`, `helm`, `jq`, `envsubst` (gettext), `cfn-lint`, Docker with `buildx`

**Cost:** about **$2.40/hour** as built in us-east-1 (EKS control plane, 2 × m6i.large, 2 × g5.xlarge, NAT gateway, NLB). With the GPU node group at 0 it's about $0.36/hour. [Clean up](#clean-up) when you're done.

## Deploy

```bash
export AWS_REGION=us-east-1
export CLUSTER=ai-inference-gpu                                    # = CloudFormation stack name
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export MY_CIDR="$(curl -s https://checkip.amazonaws.com)/32"       # only this IP can reach the API server and NLB
export APP_IMAGE=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/chat-api:v1
```

**1. Cluster and GPU nodes** (~25–30 min)

```bash
cfn-lint cfn/eks-gpu-cluster.yaml
aws cloudformation create-stack --stack-name $CLUSTER --region $AWS_REGION \
  --template-body file://cfn/eks-gpu-cluster.yaml --capabilities CAPABILITY_IAM \
  --parameters ParameterKey=ApiAccessCidr,ParameterValue=$MY_CIDR \
               ParameterKey=GpuInstanceType,ParameterValue=g5.xlarge \
               ParameterKey=GpuDesiredSize,ParameterValue=2
aws cloudformation wait stack-create-complete --stack-name $CLUSTER --region $AWS_REGION
aws eks update-kubeconfig --name $CLUSTER --region $AWS_REGION

helm repo add nvdp https://nvidia.github.io/k8s-device-plugin && helm repo update nvdp
helm upgrade --install nvdp nvdp/nvidia-device-plugin -n nvidia-device-plugin --create-namespace --wait
kubectl get nodes -l workload=gpu -o custom-columns=NODE:.metadata.name,GPU:.status.allocatable.'nvidia\.com/gpu'
```

**2. vLLM on the GPUs** (first start ~6–7 min: 9.2 GB image pull, then model load)

```bash
kubectl create namespace ai
kubectl -n ai create secret generic llm-api-key --from-literal=key="$(openssl rand -hex 32)"
kubectl apply -f k8s/gpu/01-llm-vllm.yaml
kubectl -n ai rollout status deploy/llm --timeout=15m
```

**3. Build and push the app image**

```bash
aws ecr create-repository --repository-name chat-api --region $AWS_REGION \
  --image-scanning-configuration scanOnPush=true --image-tag-mutability IMMUTABLE
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
docker buildx build --platform linux/amd64 -t $APP_IMAGE --push chat-app/
```

**4. AWS Load Balancer Controller** (IAM in CloudFormation, EKS Pod Identity)

```bash
aws cloudformation create-stack --stack-name $CLUSTER-addons-iam --region $AWS_REGION \
  --template-body file://cfn/eks-addons-iam.yaml --capabilities CAPABILITY_IAM
aws cloudformation wait stack-create-complete --stack-name $CLUSTER-addons-iam --region $AWS_REGION

VPC_ID=$(aws cloudformation describe-stacks --stack-name $CLUSTER --region $AWS_REGION \
  --query "Stacks[0].Outputs[?OutputKey=='VpcId'].OutputValue" --output text)
helm repo add eks https://aws.github.io/eks-charts && helm repo update eks
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller --version 3.6.2 \
  -n kube-system --set clusterName=$CLUSTER --set region=$AWS_REGION --set vpcId=$VPC_ID \
  --set serviceAccount.create=true --set serviceAccount.name=aws-load-balancer-controller \
  --set nodeSelector.workload=system --wait
```

**5. Envoy Gateway, gateways, routes and the app**

```bash
helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm --version v1.10.0 \
  -n envoy-gateway-system --create-namespace --wait
envsubst '${MY_CIDR}' < k8s/gateway/01-gateways.yaml | kubectl apply -f -
kubectl apply -f k8s/gateway/02-routes.yaml
envsubst '${APP_IMAGE}' < k8s/gpu/02-chat-app.yaml | kubectl apply -f -
kubectl -n gateway get gateway
kubectl -n ai get pods -o wide
```

**6. Monitoring: Prometheus, Grafana, Alertmanager, DCGM Exporter, dashboards and alerts**

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update prometheus-community
kubectl create namespace monitoring
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --version 93.1.1 \
  -n monitoring -f k8s/monitoring/kube-prometheus-stack-values.yaml --wait --timeout 10m
for f in k8s/monitoring/0*.yaml; do kubectl apply -f $f; done

# UIs on your machine only (127.0.0.1); Grafana user admin, password:
kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 -d | pbcopy
kubectl -n monitoring port-forward --address 127.0.0.1 svc/monitoring-grafana 3000:80
kubectl -n monitoring port-forward --address 127.0.0.1 svc/monitoring-kube-prometheus-prometheus 9090:9090
```

Dashboards: `/d/gpu-dcgm` (GPU hardware), `/d/vllm-workload` (vLLM per GPU), `/d/envoy-gateways` (gateways). DCGM Exporter 4.9 uses unit-suffixed metric names (`DCGM_FI_DEV_GPU_UTIL_RATIO`, `DCGM_FI_DEV_FB_USED_BYTES`).

## Test

From inside the cluster:

```bash
kubectl -n ai run gw-test --rm -i --restart=Never --image=curlimages/curl:8.11.1 \
  --overrides='{"spec":{"nodeSelector":{"workload":"system"}}}' -- \
  curl -s -X POST http://public-gw.envoy-gateway-system.svc.cluster.local/v1/ask \
  -H 'Content-Type: application/json' -d '{"question":"What does a Kubernetes Gateway do?","max_tokens":60}'
```

From your machine, without the NLB (binds to 127.0.0.1 only):

```bash
scripts/bypass-url.sh        # leave running; PORT=9090 scripts/bypass-url.sh to change the port
curl -s -X POST http://127.0.0.1:8080/v1/ask -H 'Content-Type: application/json' -d '{"question":"hi"}'
```

Through the NLB (from the `MY_CIDR` IP only):

```bash
NLB=$(kubectl -n gateway get gateway public-gw -o jsonpath='{.status.addresses[0].value}')
curl -s -X POST http://$NLB/v1/ask -H 'Content-Type: application/json' -d '{"question":"hi"}'
```

Expected behavior: `/v1/ask` returns 200, and the raw model API (`/v1/models`) returns 404 publicly. Bursts above the edge rate limit get **429**. Above 32 in-flight requests per chat-api Pod, callers get **503** with `Retry-After: 1`.

## Results (2 × A10G, Qwen2.5-0.5B, `max_tokens` 200)

| Concurrent requests | req/s | Output tokens/s | p50 | p95 | 503 |
| --- | --- | --- | --- | --- | --- |
| 1 | 1.5 | 305 | 0.65 s | 0.66 s | 0 |
| 16 | 20.2 | 3,952 | 0.78 s | 0.80 s | 0 |
| 32 | 35.0 | 6,745 | 0.85 s | 1.01 s | 0 |
| 64 | 51.2 | 9,835 | 1.05 s | 1.16 s | 14% |

Time to first token was about 27 ms and inter-token latency about 4.2 ms. Least-request routing split requests evenly across the two GPUs (433 / 432).

## Scale GPUs to zero

```bash
aws cloudformation update-stack --stack-name $CLUSTER --region $AWS_REGION \
  --use-previous-template --capabilities CAPABILITY_IAM \
  --parameters ParameterKey=GpuDesiredSize,ParameterValue=0 \
    ParameterKey=ApiAccessCidr,UsePreviousValue=true ParameterKey=GpuInstanceType,UsePreviousValue=true
# list every other parameter as UsePreviousValue=true, or it resets to its default
```

## Clean up

Delete the Gateways **first**. The Load Balancer Controller removes the NLB only while it's running. An orphaned NLB keeps billing and blocks VPC deletion.

```bash
kubectl delete -f k8s/gateway/02-routes.yaml
envsubst '${MY_CIDR}' < k8s/gateway/01-gateways.yaml | kubectl delete -f -
aws elbv2 describe-load-balancers --region $AWS_REGION \
  --query "LoadBalancers[?contains(LoadBalancerName,'k8s-envoygat')].LoadBalancerName"   # wait until []

for f in k8s/monitoring/0*.yaml; do kubectl delete -f $f; done
helm uninstall monitoring -n monitoring
kubectl delete namespace monitoring
kubectl get crd -o name | grep monitoring.coreos.com | xargs kubectl delete

helm uninstall eg -n envoy-gateway-system
helm uninstall aws-load-balancer-controller -n kube-system
helm uninstall nvdp -n nvidia-device-plugin
kubectl delete namespace ai gateway

aws cloudformation delete-stack --stack-name $CLUSTER-addons-iam --region $AWS_REGION
aws cloudformation wait stack-delete-complete --stack-name $CLUSTER-addons-iam --region $AWS_REGION
aws cloudformation delete-stack --stack-name $CLUSTER --region $AWS_REGION
aws cloudformation wait stack-delete-complete --stack-name $CLUSTER --region $AWS_REGION
aws ecr delete-repository --repository-name chat-api --region $AWS_REGION --force
```

## Security notes

- The EKS public API endpoint and the NLB accept traffic only from `MY_CIDR`. The model API is never routed publicly, and vLLM requires an API key even inside the cluster.
- There's no authentication or TLS on `/v1/ask`. Before opening it to more than one IP, add JWT validation (an Envoy Gateway `SecurityPolicy`) and HTTPS (ACM).
- Nodes enforce IMDSv2 and encrypted gp3 volumes. The app runs as non-root with a read-only root filesystem.
