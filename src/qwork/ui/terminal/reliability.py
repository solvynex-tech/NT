"""Bounded UI work and read-only process ownership, independent of accounting."""

import json
import math
import os
import time
import uuid
from pathlib import Path
from threading import Condition, Thread

import psutil


class LatestWorker:
    """One running job and at most one replacement, never an unbounded queue."""

    def __init__(self, name):
        self.name = name
        self.condition = Condition()
        self.pending = None
        self.active = False
        self.closed = False
        self.thread = None
        self.last_error = None

    def submit(self, work):
        with self.condition:
            if self.closed:
                raise ValueError("后台作业已关闭")
            self.pending = work
            if self.thread is None:
                self.thread = Thread(target=self._run, name=self.name, daemon=True)
                self.thread.start()
            self.condition.notify_all()

    def _run(self):
        while True:
            with self.condition:
                while self.pending is None and not self.closed:
                    self.condition.wait()
                if self.closed:
                    return
                work, self.pending = self.pending, None
                self.active = True
            try:
                work()
            except Exception as exc:
                self.last_error = type(exc).__name__
            finally:
                with self.condition:
                    self.active = False
                    self.condition.notify_all()

    def status(self):
        with self.condition:
            return dict(running=self.active, pending=int(self.pending is not None), closed=self.closed)

    def close(self, timeout=1):
        with self.condition:
            self.closed = True
            self.pending = None
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout)


class OwnedProcess:
    """Verify creation time and full argv each time; never terminate a process."""

    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.returncode = None
        self.uncertain = False

    @property
    def pid(self):
        return self.descriptor["members"][0]["pid"]

    def poll(self):
        self.uncertain = False
        for member in self.descriptor["members"]:
            try:
                process = psutil.Process(member["pid"])
                if abs(process.create_time() - member["created"]) > 0.01:
                    continue
                if (
                    process.cmdline() == self.descriptor["argv"]
                    and process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE
                ):
                    self.uncertain = False
                    return None
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                self.uncertain = True
        self.returncode = None if self.uncertain else -1
        return self.returncode


class ProcessRegistry:
    """Private launch metadata; no orders, positions or broker credentials."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.directory = self.data_dir / "private" / "terminal" / "processes"

    def capture(self, process, argv, context):
        if type(process.pid) is not int or process.poll() is not None:
            return None
        root = psutil.Process(process.pid)
        members = [root]
        # Windows venv launcher can own the real Python worker. Record both.
        for _ in range(3):
            children = root.children(recursive=True)
            if children:
                members.extend(children)
                break
            time.sleep(0.02)
        descriptor = dict(
            version=1,
            id=uuid.uuid4().hex,
            argv=argv,
            context=context,
            members=[dict(pid=item.pid, created=item.create_time()) for item in members if item.cmdline() == argv],
        )
        if not descriptor["members"]:
            raise ValueError("无法核验后台进程身份")
        self.validate(descriptor)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{context['mode']}-{context['account']}.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(descriptor, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return descriptor

    def validate(self, descriptor):
        from .workflow import strategy_parameters

        if descriptor.get("version") != 1:
            raise ValueError("Unknown process record")
        context = descriptor["context"]
        config = context["config"]
        keys = ["account", "mode", "margin", "positions", "execution", "symbol", "loss", "drawdown"]
        normalized = strategy_parameters(*(config[key] for key in keys))
        if normalized != config or any(context[key] != config[key] for key in ["account", "mode", "symbol"]):
            raise ValueError("Process parameters disagree")
        output = Path(context["output"]).resolve()
        if not output.is_relative_to(self.data_dir / "trading"):
            raise ValueError("Process output is outside trading directory")
        argv = descriptor["argv"]
        if not isinstance(argv, list) or len(argv) != 22 or argv[1:4] != ["-m", "qwork", "trade"]:
            raise ValueError("Not an owned trading command")
        expected = dict(zip(argv[4::2], argv[5::2], strict=True))
        if set(expected) != {
            "--mode",
            "--account-id",
            "--symbol",
            "--margin",
            "--max-positions",
            "--execution",
            "--daily-loss-limit",
            "--drawdown-limit",
            "--output",
        }:
            raise ValueError("Unexpected process arguments")
        parsed = strategy_parameters(
            expected["--account-id"],
            expected["--mode"],
            float(expected["--margin"]),
            int(expected["--max-positions"]),
            expected["--execution"],
            expected["--symbol"],
            float(expected["--daily-loss-limit"]),
            float(expected["--drawdown-limit"]),
        )
        if (
            parsed != normalized
            or Path(expected["--output"]).resolve() != output
            or Path(argv[0]).name.lower() not in {"python.exe", "python", "python3"}
        ):
            raise ValueError("Process command does not match registered intent")
        if not isinstance(descriptor["members"], list) or not 1 <= len(descriptor["members"]) <= 8:
            raise ValueError("Invalid process identity")
        for member in descriptor["members"]:
            if type(member["pid"]) is not int or member["pid"] <= 0 or not math.isfinite(member["created"]):
                raise ValueError("Invalid process identity")
        return descriptor

    def discover(self):
        active, problems = [], []
        for path in list(self.directory.glob("*.json"))[:64]:
            try:
                descriptor = self.validate(json.loads(path.read_text(encoding="utf-8")))
                process = OwnedProcess(descriptor)
                if process.poll() is None:
                    if process.uncertain:
                        problems.append("后台进程身份暂不可核验")
                    else:
                        active.append((process, descriptor["context"]))
            except (ValueError, KeyError, TypeError, OSError):
                problems.append("后台进程恢复记录不可读取，需核验已有策略")
        return active, problems
