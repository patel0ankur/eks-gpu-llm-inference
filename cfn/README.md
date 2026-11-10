# EKS GPU Cluster (CloudFormation)

`eks-gpu-cluster.yaml` creates:

| Resource | Details |
| --- | --- |
| VPC | 3 AZs · 3 public /20 subnets (load balancers, NAT) · 3 private /19 subnets (nodes, Pods) · 1 NAT gateway · subnets tagged for the AWS Load Balancer Controller |
| EKS cluster | Name = stack name · private + public API endpoint (public restricted to `ApiAccessCidr`) · secrets encrypted with a rotating KMS key · api/audit/authenticator logs (30-day retention) · access entries (creator is admin) |
| IAM | Cluster role, node role (ECR read-only, CNI, SSM Session Manager), OIDC provider |
| Add-ons | vpc-cni, kube-proxy, coredns, eks-pod-identity-agent |
| System node group | `AL2023_x86_64_STANDARD`, m6i.large × 2, for CoreDNS, controllers and monitoring |
| GPU node group | `AL2023_x86_64_NVIDIA` (or Bottlerocket NVIDIA), g5.xlarge by default, 0–2 nodes, labelled `workload=gpu`, tainted `nvidia.com/gpu=true:NoSchedule` |

Both node groups use launch templates that enforce **IMDSv2** and **encrypted gp3** root volumes. No security group allows inbound `0.0.0.0/0`.

## Deploy

```bash
export AWS_REGION=us-east-1
export STACK=ai-inference-gpu                     # also the cluster name
export MY_CIDR="$(curl -s https://checkip.amazonaws.com)/32"

# Check GPU quota first: "Running On-Demand G and VT instances" (vCPUs). g5.xlarge needs 4.
aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA \
  --region $AWS_REGION --query Quota.Value

cfn-lint eks-gpu-cluster.yaml

aws cloudformation create-stack --stack-name $STACK --region $AWS_REGION \
  --template-body file://eks-gpu-cluster.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters ParameterKey=ApiAccessCidr,ParameterValue=$MY_CIDR \
               ParameterKey=GpuInstanceType,ParameterValue=g5.xlarge

aws cloudformation wait stack-create-complete --stack-name $STACK --region $AWS_REGION   # ~20-25 min
aws eks update-kubeconfig --name $STACK --region $AWS_REGION
kubectl get nodes -L workload,node.kubernetes.io/instance-type
```

If creation fails, list only the failure events:

```bash
aws cloudformation describe-events --stack-name $STACK --region $AWS_REGION --filters FailedEvents=true
```

The common causes are GPU quota (`InsufficientInstanceCapacity` / `VcpuLimitExceeded`) or the GPU type not being offered in one of the three AZs. Try another instance type or Region.

## Enable GPUs in Kubernetes

The AL2023 NVIDIA AMI includes the driver and container toolkit. Kubernetes still needs the **NVIDIA device plugin** to advertise `nvidia.com/gpu` (skip this step for Bottlerocket NVIDIA, which has it built in):

```bash
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin && helm repo update nvdp
helm upgrade --install nvdp nvdp/nvidia-device-plugin -n nvidia-device-plugin --create-namespace
kubectl get nodes -l workload=gpu -o custom-columns=NODE:.metadata.name,GPU:.status.allocatable.'nvidia\.com/gpu'
```

Test a GPU Pod (it needs the toleration because the GPU nodes are tainted):

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata: {name: gpu-test}
spec:
  restartPolicy: Never
  nodeSelector: {workload: gpu}
  tolerations: [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
  containers:
    - name: cuda
      image: nvidia/cuda:12.4.1-base-ubuntu22.04
      command: ["nvidia-smi"]
      resources: {limits: {nvidia.com/gpu: 1}}
EOF
kubectl logs -f gpu-test        # shows the GPU table
kubectl delete pod gpu-test
```

## Next

Continue with the vLLM, app, Load Balancer Controller and Envoy Gateway steps in the [top-level README](../README.md). The LB Controller's IAM is in [`eks-addons-iam.yaml`](eks-addons-iam.yaml) (EKS Pod Identity, no IRSA needed).

## Scale the GPU node group

Scale to 0 when idle to stop GPU charges (the system nodes and control plane keep running):

```bash
aws cloudformation update-stack --stack-name $STACK --region $AWS_REGION \
  --use-previous-template --capabilities CAPABILITY_IAM \
  --parameters ParameterKey=ApiAccessCidr,UsePreviousValue=true \
               ParameterKey=GpuDesiredSize,ParameterValue=0
```

Parameters you leave out of `update-stack` go back to their defaults, so add `ParameterKey=<Name>,UsePreviousValue=true` for every other value you set at create time (for example `GpuInstanceType`).

## Delete

Delete any Kubernetes `LoadBalancer` Services and Ingresses first (otherwise their NLBs/ALBs block VPC deletion), then:

```bash
aws cloudformation delete-stack --stack-name $STACK --region $AWS_REGION
aws cloudformation wait stack-delete-complete --stack-name $STACK --region $AWS_REGION
```

## Cost (approx., us-east-1)

EKS control plane ~$0.10/h · 2 × m6i.large ~$0.19/h · NAT gateway ~$0.045/h + data · **g5.xlarge ~$1.01/h each**. Check current pricing for your Region.
