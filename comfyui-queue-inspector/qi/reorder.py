"""Opt-in queue reordering (enabled with --allow-reorder).

ComfyUI has no "move" API. A pending job is moved by deleting it and queuing
the identical job again with a chosen `number`; execution order is ascending
`number` (a float), so a job can be slotted between two others with the
midpoint of their numbers.

Faithful re-queue (verified against ComfyUI server.py / execution.py, 2026-11):

* prompt_id   - POST /prompt accepts the original id ("prompt_id").
* prompt      - The queued prompt is the copy that validation already edited
                in place: widget values wrapped as {"__value__": [...]} were
                unwrapped to bare lists, which a second validation would take
                for node links. Non-link lists are wrapped again, but only in
                nodes that feed the outputs (the only nodes validated).
                Number/string coercion and node replacement are idempotent.
* outputs     - Sent as partial_execution_targets, so a job queued for some
                outputs only does not turn into a full run.
* extra_data  - Sent back as is. The server drops and rebuilds
                "workflow_metadata", so it is also sent at the top level, and
                "client_id" too so the job keeps reporting to the same tab.
                Only "create_time" changes (the server always stamps it).
* sensitive   - Comfy.org login tokens of API-node jobs are not exposed by
                /queue, so jobs using API nodes are never moved.

Every move is verified by reading /queue back and comparing with the
original; a job whose re-queue is rejected is put back at its old number.
A JSON backup of each job is written before it is deleted.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .adapter import RawQueueItem, parse_queue
from .catalog import NodeCatalog
from .comfy_client import ComfyClient, ComfyUnavailable
from .extract import is_link, used_nodes

# Smallest step between numbers we hand out. Repeatedly inserting into the same
# slot halves the gap each time; below this we also move a neighbour instead,
# which restores a wide gap (far from float precision limits).
MIN_GAP = 1e-6


class PlanError(ValueError):
    pass


# ---------------------------------------------------------------- planning

def merge_draft(current: Sequence[str], draft: Sequence[str]) -> List[str]:
    """Final order: draft jobs take the draft's order inside the slots they
    occupy now; jobs the draft does not mention (queued while editing) keep
    their slot. Draft ids no longer pending are ignored."""
    current_set = set(current)
    wanted = [pid for pid in dict.fromkeys(draft) if pid in current_set]
    wanted_set = set(wanted)
    it = iter(wanted)
    return [next(it) if pid in wanted_set else pid for pid in current]


def _keep_set(current: Sequence[str], desired: Sequence[str], pinned: Set[str]) -> Set[str]:
    """Largest set of jobs whose relative order is unchanged (longest
    increasing subsequence of their current positions). Pinned jobs must be
    kept: they get a weight larger than all others together."""
    pos = {pid: i for i, pid in enumerate(current)}
    seq = [pos[pid] for pid in desired]
    big = len(seq) + 1
    weight = [big if desired[i] in pinned else 1 for i in range(len(seq))]
    best = weight[:]
    prev = [-1] * len(seq)
    for i in range(len(seq)):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + weight[i] > best[i]:
                best[i] = best[j] + weight[i]
                prev[i] = j
    if not seq:
        return set()
    i = max(range(len(seq)), key=lambda k: best[k])
    keep = set()
    while i != -1:
        keep.add(desired[i])
        i = prev[i]
    missing = pinned & set(desired) - keep
    if missing:
        raise PlanError("jobs that cannot be moved would change order: " + ", ".join(sorted(missing)))
    return keep


def plan_moves(current: Sequence[Tuple[str, float]], desired: Sequence[str],
               pinned: Set[str] = frozenset()) -> List[Tuple[str, float]]:
    """Return [(prompt_id, new_number)] - the fewest re-queues that turn the
    `current` pending order (execution order, with numbers) into `desired`."""
    ids = [pid for pid, _ in current]
    if sorted(ids) != sorted(desired):
        raise PlanError("desired order must contain exactly the current pending jobs")
    number = dict(current)
    pinned = set(pinned)
    keep = _keep_set(ids, desired, pinned)
    while True:
        moves = _assign_numbers(desired, keep, number)
        if isinstance(moves, list):
            return moves
        # Gap too small around run [i, j): stop keeping a neighbour and retry.
        i, j = moves
        if j < len(desired) and desired[j] not in pinned:
            keep.discard(desired[j])
        elif i > 0 and desired[i - 1] not in pinned:
            keep.discard(desired[i - 1])
        else:
            raise PlanError("queue numbers are too close to insert between; move fewer jobs at once")


def _assign_numbers(desired, keep, number):
    """Numbers for the jobs not kept, or (i, j) of a run whose gap is too small."""
    moves: List[Tuple[str, float]] = []
    # Numbers in use by other jobs: a new number must not tie with one of them,
    # even transiently (ties are broken by prompt_id, i.e. arbitrarily).
    taken = set(number.values())
    i = 0
    while i < len(desired):
        if desired[i] in keep:
            i += 1
            continue
        j = i
        while j < len(desired) and desired[j] not in keep:
            j += 1
        run = desired[i:j]  # moved jobs between two kept neighbours
        lo = number[desired[i - 1]] if i > 0 else None
        hi = number[desired[j]] if j < len(desired) else None
        k = len(run)
        if lo is None and hi is None:
            lo, hi = 0.0, float(k + 1)
        elif lo is None:
            lo = hi - (k + 1)
        elif hi is None:
            hi = lo + (k + 1)
        step = (hi - lo) / (k + 1)
        if step < MIN_GAP:
            return (i, j)
        for n, pid in enumerate(run, start=1):
            new = lo + step * n
            nudge = step / 7.0  # stays inside (lo + step*(n-1), lo + step*(n+1))
            while new in taken and nudge > MIN_GAP / 1000:
                new += nudge
                nudge /= 2.0
            taken.add(new)
            moves.append((pid, new))
        i = j
    return moves


# ---------------------------------------------------------------- faithful re-queue

def rewrap_list_values(prompt: dict, used: Optional[Set[str]]) -> dict:
    """Undo validation's unwrapping of list widget values (see module doc)."""
    out = copy.deepcopy(prompt)
    for node_id, node in out.items():
        if used is not None and node_id not in used:
            continue
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for name, value in inputs.items():
            if isinstance(value, list) and not (is_link(value) and str(value[0]) in out):
                inputs[name] = {"__value__": value}
    return out


def requeue_body(raw: RawQueueItem, number: float) -> Dict[str, Any]:
    used = used_nodes(raw.prompt, raw.outputs)
    extra = copy.deepcopy(raw.extra_data)
    body: Dict[str, Any] = {
        "prompt": rewrap_list_values(raw.prompt, used),
        "prompt_id": raw.prompt_id,
        "number": number,
        "extra_data": extra,
    }
    if raw.outputs:
        body["partial_execution_targets"] = list(raw.outputs)
    if isinstance(extra.get("workflow_metadata"), dict):
        body["workflow_metadata"] = extra["workflow_metadata"]
    if extra.get("client_id"):
        body["client_id"] = extra["client_id"]
    return body


def compare_requeued(original: RawQueueItem, again: RawQueueItem) -> List[str]:
    """Names of the parts that differ (create_time is expected to change)."""
    diffs = []
    if again.prompt != original.prompt:
        diffs.append("prompt")
    a = {k: v for k, v in original.extra_data.items() if k != "create_time"}
    b = {k: v for k, v in again.extra_data.items() if k != "create_time"}
    if a != b:
        diffs.append("extra_data")
    if sorted(again.outputs) != sorted(original.outputs):
        diffs.append("outputs")
    return diffs


def api_node_classes(raw: RawQueueItem, catalog: NodeCatalog) -> List[str]:
    used = used_nodes(raw.prompt, raw.outputs)
    found = set()
    for node_id, node in raw.prompt.items():
        if used is not None and str(node_id) not in used:
            continue
        cls = node.get("class_type") if isinstance(node, dict) else None
        if isinstance(cls, str) and catalog.is_api_node(cls):
            found.add(cls)
    return sorted(found)


# ---------------------------------------------------------------- applying

class Reorderer:
    def __init__(self, client: ComfyClient, catalog_getter, backup_dir: str, log=print):
        self.client = client
        self.catalog_getter = catalog_getter
        self.backup_dir = backup_dir
        self.log = log
        self._lock = threading.Lock()

    def _read(self) -> Tuple[Dict[str, RawQueueItem], List[RawQueueItem]]:
        items = parse_queue(self.client.get_queue())
        running = {i.prompt_id: i for i in items if i.status == "running"}
        pending = [i for i in items if i.status == "pending"]
        return running, pending

    def _backup(self, raw: RawQueueItem) -> str:
        os.makedirs(self.backup_dir, exist_ok=True)
        path = os.path.join(self.backup_dir, "%s_%s.json" % (time.strftime("%Y%m%d-%H%M%S"), raw.prompt_id))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"number": raw.number, "prompt_id": raw.prompt_id, "prompt": raw.prompt,
                       "extra_data": raw.extra_data, "outputs_to_execute": raw.outputs}, f, ensure_ascii=False)
        return path

    def _ran(self, prompt_id: str) -> bool:
        try:
            hist = self.client.get_json("/history/" + prompt_id)
        except ComfyUnavailable:
            return True  # cannot tell: assume it ran rather than risk running it twice
        return isinstance(hist, dict) and prompt_id in hist

    def _post_prompt(self, body) -> Tuple[bool, str]:
        status, data = self.client.post_json("/prompt", body)
        if status == 200:
            return True, ""
        msg = ""
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            msg = data["error"].get("message", "")
        return False, "ComfyUI rejected the job (HTTP %d) %s" % (status, msg)

    def _move(self, raw: RawQueueItem, new_number: float) -> Dict[str, Any]:
        res = {"promptId": raw.prompt_id, "from": raw.number, "to": new_number,
               "status": "failed", "reason": "", "diffs": [], "backup": None}
        res["backup"] = self._backup(raw)

        self.client.post_json("/queue", {"delete": [raw.prompt_id]})
        running, pending = self._read()
        if raw.prompt_id in running:
            res.update(status="skipped", reason="started running before it could be moved")
            return res
        if any(p.prompt_id == raw.prompt_id for p in pending):
            res.update(status="skipped", reason="ComfyUI did not remove it from the queue")
            return res
        if self._ran(raw.prompt_id):
            res.update(status="skipped", reason="it ran (or finished) while being moved; not queued again")
            return res

        ok, why = self._post_prompt(requeue_body(raw, new_number))
        if not ok:
            # Put it back where it was.
            ok_back, why_back = self._post_prompt(requeue_body(raw, raw.number))
            if ok_back:
                res.update(status="restored", reason=why + "; left at its old position")
            else:
                res.update(status="failed", reason=why + "; restoring also failed: " + why_back)
            return res

        _, pending = self._read()
        again = next((p for p in pending if p.prompt_id == raw.prompt_id), None)
        if again is None:
            res.update(status="moved", reason="queued again but already started before it could be checked")
            return res
        res["diffs"] = compare_requeued(raw, again)
        res["status"] = "moved"
        if res["diffs"]:
            res["reason"] = "re-queued job differs from the original in: " + ", ".join(res["diffs"])
        return res

    def apply(self, draft: List[str]) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "another reorder is in progress"}
        try:
            running, pending = self._read()
            current = [(p.prompt_id, p.number) for p in pending if p.number is not None]
            by_id = {p.prompt_id: p for p in pending}
            ids = [pid for pid, _ in current]
            catalog = self.catalog_getter()
            if not catalog.available:
                return {"ok": False, "error": "node type info (/object_info) is not loaded yet, so jobs "
                                             "using cloud API nodes cannot be recognised; try again shortly"}
            blocked = {p.prompt_id for p in pending if api_node_classes(p, catalog)}
            mentioned = set(draft)
            pinned = blocked | {pid for pid in ids if pid not in mentioned}
            gone = [pid for pid in draft if pid not in by_id]
            desired = merge_draft(ids, draft)
            moves = plan_moves(current, desired, pinned)

            results = []
            for pid, new_number in moves:
                try:
                    r = self._move(by_id[pid], new_number)
                except ComfyUnavailable as exc:
                    r = {"promptId": pid, "from": by_id[pid].number, "to": new_number,
                         "status": "failed", "reason": "ComfyUI unreachable: %s" % exc, "diffs": [], "backup": None}
                self.log("[reorder] %s %s -> %s: %s %s" % (pid[:8], r["from"], r["to"], r["status"], r["reason"]))
                results.append(r)
                if r["status"] == "failed":
                    break  # stop at the first hard failure; the rest stay untouched
            return {"ok": True, "previousOrder": ids, "results": results,
                    "skippedGone": [pid for pid in gone if pid not in running] ,
                    "skippedRunning": [pid for pid in gone if pid in running]}
        except PlanError as exc:
            return {"ok": False, "error": str(exc)}
        except ComfyUnavailable as exc:
            return {"ok": False, "error": "ComfyUI unreachable: %s" % exc}
        finally:
            self._lock.release()
