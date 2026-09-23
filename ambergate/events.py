"""One bounded, shared dashboard snapshot for all connected SSE clients."""
import json
import threading


def event(name, value, sequence=None):
    prefix = f"id: {sequence}\n" if sequence is not None else ""
    return (prefix + f"event: {name}\ndata: " +
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()


class DashboardEvents:
    def __init__(self, snapshot, interval=1, capacity=16):
        self.snapshot = snapshot
        self.interval = interval
        self.capacity = capacity
        self.condition = threading.Condition()
        self.clients = 0
        self.sequence = 0
        self.frame = None
        self.stopped = False
        self.thread = None

    def subscribe(self):
        with self.condition:
            if self.stopped or self.clients >= self.capacity:
                return False
            if not self.clients:
                self.frame = None  # Never replay a snapshot from an idle period.
            self.clients += 1
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="ambergate-events", daemon=True)
                self.thread.start()
            self.condition.notify_all()
            return True

    def unsubscribe(self):
        with self.condition:
            self.clients -= 1
            self.condition.notify_all()

    def wait(self, sequence):
        with self.condition:
            self.condition.wait_for(lambda: self.stopped or
                self.frame is not None and self.sequence != sequence, timeout=1)
            return self.sequence, self.frame if self.sequence != sequence else None

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self.clients)
                if self.stopped:
                    return
            try:
                frame = event("dashboard", self.snapshot(), self.sequence + 1)
            except Exception as exc:
                print(f"dashboard stream: {type(exc).__name__}: {exc}", flush=True)
                frame = event("stream_error", {"error": "Не удалось получить состояние AmberGate"})
            with self.condition:
                if self.stopped:
                    return
                self.sequence += 1
                self.frame = frame
                self.condition.notify_all()
                self.condition.wait_for(lambda: self.stopped or not self.clients, timeout=self.interval)

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=2)
