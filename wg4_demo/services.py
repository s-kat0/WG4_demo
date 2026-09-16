"""Application service composition; no API client is cached here."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wg4_demo.agent_runtime import AgentService, PromptStore, StructuredWorkflowService
from wg4_demo.approvals import ApprovalService
from wg4_demo.auth import AuthService
from wg4_demo.conversation import ConversationService
from wg4_demo.evidence import EvidenceService
from wg4_demo.export import ExportService
from wg4_demo.graph import GraphService
from wg4_demo.job_runner import ApplicationJobRunner
from wg4_demo.jobs import JobService
from wg4_demo.llm_gateway import LLMGateway
from wg4_demo.repository import Repository
from wg4_demo.result_validation import ResultValidator
from wg4_demo.retrieval import RetrievalService
from wg4_demo.scheduler import Scheduler
from wg4_demo.settings import Settings
from wg4_demo.usage_ledger import UsageLedger


@dataclass(slots=True)
class Services:
    settings: Settings
    auth: AuthService
    repository: Repository
    ledger: UsageLedger
    jobs: JobService
    scheduler: Scheduler
    retrieval: RetrievalService
    graph: GraphService
    evidence: EvidenceService
    conversations: ConversationService
    approvals: ApprovalService
    exporter: ExportService


def build_services(settings: Settings, *, project_root: Path) -> Services:
    repository = Repository(settings.knowledge_db_path)
    auth = AuthService(settings.control_db_path, settings)
    ledger = UsageLedger(settings.control_db_path, settings, auth)
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")
    graph = GraphService(repository)
    evidence = EvidenceService(repository)
    conversations = ConversationService(repository)
    validator = ResultValidator(repository)
    gateway = LLMGateway(settings, ledger)
    prompts = PromptStore(project_root / "prompts")
    structured = StructuredWorkflowService(gateway, repository, prompts)
    agents = AgentService(
        settings,
        gateway,
        repository,
        retrieval,
        graph,
        evidence,
        validator,
        prompts,
    )
    jobs = JobService(settings.control_db_path, settings, auth, repository)
    runner = ApplicationJobRunner(jobs, structured, agents)
    scheduler = Scheduler(
        jobs,
        repository,
        {
            "extract": runner,
            "interview": runner,
            "supplement": runner,
            "reflect": runner,
            "qa": runner,
            "update": runner,
        },
        max_workers=settings.max_concurrent_jobs,
    )
    scheduler.start()
    return Services(
        settings=settings,
        auth=auth,
        repository=repository,
        ledger=ledger,
        jobs=jobs,
        scheduler=scheduler,
        retrieval=retrieval,
        graph=graph,
        evidence=evidence,
        conversations=conversations,
        approvals=ApprovalService(repository, auth),
        exporter=ExportService(repository, auth),
    )
