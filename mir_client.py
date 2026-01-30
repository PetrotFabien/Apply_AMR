
import os, base64, time
import requests

class MiRClient:
    """Client MiR REST API avec mode Dry-Run.
    Endpoints utilisés: /status, /missions, /mission_queue.
    Dry-Run activé si MIR_DRY_RUN=true.
    """
    def __init__(self, base=None, user=None, password=None, verify=False, timeout=8):
        self.dry = (os.getenv("MIR_DRY_RUN", "false").lower() == "true")
        self.base = (base or os.getenv("MIR_BASE_URL", "")).rstrip("/")
        self.user = user or os.getenv("MIR_USER", "dry")
        self.password = password or os.getenv("MIR_PASS", "run")
        self.verify = verify
        self.timeout = timeout
        self._t0 = time.time()

        if not self.dry:
            if not self.base:
                raise RuntimeError("MIR_BASE_URL manquant")
            if not self.user or not self.password:
                raise RuntimeError("MIR_USER/MIR_PASS manquants")
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            self.headers = {
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
                "Accept-Language": "en-US",
            }

    # --- helpers HTTP ---
    def _get(self, path):
        if self.dry:
            return {"dry_run": True, "endpoint": path}
        r = requests.get(f"{self.base}{path}", headers=self.headers,
                         timeout=self.timeout, verify=self.verify)
        r.raise_for_status()
        return r.json()

    def _post(self, path, payload):
        if self.dry:
            return {"dry_run": True, "endpoint": path, "payload": payload}
        r = requests.post(f"{self.base}{path}", json=payload, headers=self.headers,
                          timeout=self.timeout, verify=self.verify)
        r.raise_for_status()
        return r.json() if r.text else {}

    # --- API MiR ---
    def status(self):
        if self.dry:
            elapsed = time.time() - self._t0
            battery = max(5, 100 - int(elapsed) % 100)
            return {
                "dry_run": True,
                "robot_name": "MiR250-Demo",
                "state_text": "Ready" if int(elapsed)%10<7 else "Executing mission",
                "mission_text": "Moving to Emballage" if int(elapsed)%10>=7 else "Waiting for new missions...",
                "battery_percentage": battery,
                "position": {"x": round(1.0 + 0.01*elapsed,2), "y": round(2.0 + 0.02*elapsed,2), "orientation": 0.0},
            }
        return self._get("/status")

    def missions(self):
        if self.dry:
            return [
                {"name":"POSTE-PHOTO","guid":"11111111-2222-3333-4444-555555555555"},
                {"name":"POSTE-INSPECTION","guid":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                {"name":"POSTE-EMBALLAGE","guid":"99999999-8888-7777-6666-555555555555"},
                {"name":"RETOUR-BASE","guid":"bbbbbbbb-cccc-dddd-eeee-ffffffffffff"}
            ]
        return self._get("/missions")

    def start_mission(self, mission_guid: str):
        return self._post("/mission_queue", {"mission_id": mission_guid})
