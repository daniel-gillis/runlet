#!/usr/bin/env python3
"""Start runners as Kubernetes pods instead of docker containers.

The controller runs inside the cluster it provisions for, so it cannot talk to a
Docker daemon. This adapter uses the pod's service account to create one pod per
job and reads the same API to decide what is still running.

The scheduler treats it exactly like the docker runtime: `running()` returns the
names of live runners, `start()` creates one and returns whether it did.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

POD_TEMPLATE = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": "",
        "labels": {"app": "runlet-runner"},
        "annotations": {},
    },
    "spec": {
        "restartPolicy": "Never",
        # A finished job should not linger; the controller deletes pods it owns.
        "terminationGracePeriodSeconds": 10,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [{
            "name": "runner",
            "image": "",
            "imagePullPolicy": "IfNotPresent",
            "env": [],
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "4", "memory": "4Gi"},
            },
        }],
    },
}


class KubernetesRuntime:
    def __init__(self, namespace: str, image: str, service_account: str = "",
                 api: str = "https://kubernetes.default.svc", prefix: str = "runlet-"):
        self.namespace = namespace
        self.image = image
        self.service_account = service_account
        self.api = api.rstrip("/")
        self.prefix = prefix
        self.token = (SA_DIR / "token").read_text().strip() if (SA_DIR / "token").exists() else ""
        ca = SA_DIR / "ca.crt"
        self.context = ssl.create_default_context(cafile=str(ca)) if ca.exists() else ssl.create_default_context()

    # ── API plumbing ────────────────────────────────────────────────────────
    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.api + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {}),
        })
        try:
            with urllib.request.urlopen(request, timeout=20, context=self.context) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {path} -> {error.code}: {detail}") from None

    # ── Runtime interface ───────────────────────────────────────────────────
    def running(self) -> list[str]:
        """Names of pods that are not finished.

        A pod in Succeeded or Failed has taken its job (or died), so the scheduler
        should see it as gone and reconcile from the job's upstream state.
        """
        data = self._call("GET", f"/api/v1/namespaces/{self.namespace}/pods"
                                 f"?labelSelector=app%3Drunlet-runner")
        names = []
        for pod in data.get("items", []):
            if not pod["metadata"]["name"].startswith(self.prefix):
                continue
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            names.append(pod["metadata"]["name"])
        return names

    def start(self, runner_name: str, labels: tuple[str, ...], fencing_token: int, repo: str) -> bool:
        pod = json.loads(json.dumps(POD_TEMPLATE))  # deep copy
        pod["metadata"]["name"] = runner_name
        pod["metadata"]["labels"]["runlet/labels"] = ",".join(labels)
        pod["metadata"]["labels"]["runlet/repo"] = repo.replace("/", "_")[:63]
        pod["metadata"]["annotations"]["runlet/fencing-token"] = str(fencing_token)
        if self.service_account:
            pod["spec"]["serviceAccountName"] = self.service_account

        container = pod["spec"]["containers"][0]
        container["image"] = self.image
        try:
            jit = self._mint(repo, runner_name, labels)
        except RuntimeError as error:
            print(f"  ! {error}", flush=True)
            return False
        container["env"] = [
            {"name": "RUNNER_JIT_CONFIG", "value": jit},
            {"name": "RUNNER_ALLOW_RUNASROOT", "value": "1"},
        ]

        try:
            self._call("POST", f"/api/v1/namespaces/{self.namespace}/pods", pod)
        except RuntimeError as error:
            message = str(error)
            if "AlreadyExists" in message:
                return True  # a previous pass already created it
            print(f"  ! could not create pod {runner_name}: {message}", flush=True)
            return False
        return True

    def reap(self) -> int:
        """Delete finished pods. A pod that has taken its job should not linger."""
        data = self._call("GET", f"/api/v1/namespaces/{self.namespace}/pods"
                                 f"?labelSelector=app%3Drunlet-runner")
        removed = 0
        for pod in data.get("items", []):
            name = pod["metadata"]["name"]
            if not name.startswith(self.prefix):
                continue
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                self.remove(name)
                removed += 1
        return removed

    def remove(self, runner_name: str) -> None:
        try:
            self._call("DELETE", f"/api/v1/namespaces/{self.namespace}/pods/{runner_name}")
        except RuntimeError:
            pass

    # ── JIT minting ─────────────────────────────────────────────────────────
    def _mint(self, repo: str, runner_name: str, labels: tuple[str, ...]) -> str:
        """Mint through the same public API the docker runtime uses."""
        import os
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        body = json.dumps({
            "name": runner_name,
            "runner_group_id": 1,
            "work_folder": "_work",
            "labels": list(labels),
        }).encode()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/actions/runners/generate-jitconfig",
            data=body, method="POST", headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "runletd/0.3",
            })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)["encoded_jit_config"]
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"generate-jitconfig for {runner_name} -> {error.code}: {detail}") from None
