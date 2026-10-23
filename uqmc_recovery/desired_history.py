"""Run-local, append-only desired history index under the existing journal lock."""
import copy


class DesiredHistory:
    def __init__(self, journal, plan_sha256):
        self.journal, self.plan_sha256 = journal, plan_sha256
        self.epoch = None
        self.cursor = 0
        self.all = []
        self.by_source = {}

    def events(self, source_id=None):
        # Journal's gate also serializes shared worker access to this index.
        # A new lock lifetime rebuilds it from the newly validated chain.
        with self.journal.locked():
            epoch = getattr(self.journal, '_lock_epoch', None)
            if epoch is None or epoch != self.epoch:
                self.epoch, self.cursor = epoch, 0
                self.all, self.by_source = [], {}
            tail = self.journal.records_after(self.cursor)
            for event in tail:
                payload = event['payload']
                if event['kind'].startswith('DESIRED_') and payload.get('plan_sha256') == self.plan_sha256:
                    self.all.append(event)
                    self.by_source.setdefault(payload.get('source_id'), []).append(event)
            if tail:
                self.cursor = tail[-1]['seq']
            return copy.deepcopy(self.all if source_id is None else self.by_source.get(source_id, []))
