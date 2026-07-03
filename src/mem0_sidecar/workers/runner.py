from mem0_sidecar.store.repositories import JobRepository


class WorkerRunner:
    def __init__(self, jobs: JobRepository) -> None:
        self.jobs = jobs

    def run_once(self) -> bool:
        job = self.jobs.claim_next()
        return job is not None
