# Running in Kubernetes

The controller can start runners as pods instead of containers on a Docker host. Use it when the controller itself runs inside the cluster it provisions for, where there is no Docker daemon to talk to.

## What changes

| | Docker runtime | Kubernetes runtime |
| --- | --- | --- |
| Starts a runner with | `docker run` | a pod, through the cluster API |
| Authentication | local Docker socket | the controller's service account |
| Sees what is running | `docker ps` | a label-scoped pod list |
| Cleanup | `--rm` on exit | the controller deletes finished pods |

The scheduler is unchanged. Both runtimes implement the same three methods, so leases, fencing and reconciliation behave identically.

## Permissions

The controller needs permission to manage pods in the namespace where runners run, and nothing else. It does not need cluster-wide access, and the runner pods get a service account with no permissions of their own.

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: runlet-controller
  namespace: RUNNER_NAMESPACE
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch", "create", "delete"]
```

Binding it to the controller's service account, which may live in a different namespace:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: runlet-controller
  namespace: RUNNER_NAMESPACE
subjects:
  - kind: ServiceAccount
    name: runlet-controller
    namespace: CONTROLLER_NAMESPACE
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: runlet-controller
```

## Use a separate namespace for runners

Runner pods are short-lived and bursty, which makes them a poor fit for an application quota. Quotas count declared limits rather than usage, so a namespace full of long-running services with generous limits can have no room for a runner even when the nodes are idle.

Give runners their own namespace and their own budget:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: RUNNER_NAMESPACE
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: runlet-quota
  namespace: RUNNER_NAMESPACE
spec:
  hard:
    pods: "24"
    requests.cpu: "6"
    requests.memory: "12Gi"
    limits.cpu: "24"
    limits.memory: "24Gi"
```

The controller then runs with `--namespace RUNNER_NAMESPACE`, and `--service-account` names the account given to each runner pod.

## Pin the controller image

Clusters commonly run an admission policy that rejects floating tags. Pin the controller by version or, better, by commit:

```yaml
image: ghcr.io/OWNER/runlet-controller:sha-COMMIT
```

The publish workflow tags each build with `sha-<full commit>`, so the manifest names exactly the build that is deployed.

## Expose the webhook

GitHub delivers `workflow_job` events over HTTPS, so the endpoint has to be reachable from the internet. Two details matter:

**The webhook authenticates itself.** Requests carry `X-Hub-Signature-256`, an HMAC over the body using the secret from the hook configuration. The controller verifies it and answers 401 to anything else, so the endpoint needs no other authentication.

**Anything in front of it will block it by default.** If the host sits behind an SSO proxy, a login redirect will stop every delivery before it reaches the controller. Exempt the path in that proxy, for example with oauth2-proxy:

```
--skip-auth-route=^/PREFIX/hook$
```

and route the path through to the controller's Service. Deliveries that reach it return 204; a redirect to a login page means the exemption is missing.

## Run the controller

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: runlet-controller
  namespace: CONTROLLER_NAMESPACE
spec:
  replicas: 1                 # two controllers would race on the same jobs
  strategy: { type: Recreate }
  selector:
    matchLabels: { app.kubernetes.io/name: runlet-controller }
  template:
    metadata:
      labels: { app.kubernetes.io/name: runlet-controller }
    spec:
      serviceAccountName: runlet-controller
      securityContext: { runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000 }
      containers:
        - name: controller
          image: ghcr.io/OWNER/runlet-controller:sha-COMMIT
          args:
            - --repo=OWNER/REPO
            - --serve=runlet-runner
            - --image=ghcr.io/OWNER/runlet-runner:0.1.0
            - --runtime=kubernetes
            - --namespace=RUNNER_NAMESPACE
            - --service-account=runlet-runner
            - --state=/var/lib/runlet/ledger.db
            - --max=4
            - --metrics-port=9187
            - --webhook-port=8787
          env:
            - name: RUNLET_NAMESPACE
              value: RUNNER_NAMESPACE
            - name: RUNLET_IMAGE
              value: ghcr.io/OWNER/runlet-runner:0.1.0
          envFrom:
            - secretRef: { name: runlet }     # GITHUB_TOKEN, RUNLETD_WEBHOOK_SECRET
          volumeMounts:
            - { name: ledger, mountPath: /var/lib/runlet }
      volumes:
        - name: ledger
          persistentVolumeClaim: { claimName: runlet-ledger }
```

`RUNLET_NAMESPACE` and `RUNLET_IMAGE` exist for the status command, which otherwise looks for runner pods in the namespace the controller runs in.

## Verify

```console
$ kubectl -n CONTROLLER_NAMESPACE exec deploy/runlet-controller -- \
    python3 /app/status.py --state /var/lib/runlet/ledger.db --runtime kubernetes
```

`--runtime kubernetes` asks the API which runner pods are alive instead of calling a Docker daemon that does not exist in the pod.

Dispatch a job that only these runners can serve, and watch for the pod:

```console
$ kubectl -n RUNNER_NAMESPACE get pods -w
```

The controller logs one line per action, naming the repository and job:

```
webhook: job queued
23:08:52  dispatching OWNER/REPO "build" to runlet-123456789-1-4f2c1a (labels ['runlet-runner'], attempt 1)
23:09:31  finished OWNER/REPO "build" after 39s (runlet-123456789-1-4f2c1a)
```

## Limits

- Every job pays for a pod: image pull on a cold node, then pod scheduling and container start. A warm pool (`--min-warm`) helps only if the image is already present on the node.
- Jobs that use Docker actions need access to a container runtime. The runner pod has none by default, so those jobs fail unless you add a socket or a rootless builder.
- The controller holds the GitHub token and creates pods, so treat it as a privileged workload.
- One controller per repository set. It is not leader-elected, which is why the Deployment runs a single replica with the `Recreate` strategy.
