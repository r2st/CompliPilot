"""Celery application, beat schedule, and the hooks that carry logging context.

The design document names BullMQ. BullMQ is a Node library and this backend is
Python; Celery drives the same Redis with the same semantics, and running one
language in the service is worth more than matching the document's word.

Tasks are eager when ``CELERY_ENABLED`` is false, so the test suite and a
laptop with no broker run the task body inline instead of hanging on a
connection that is not there.
"""
from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun, task_prerun

from app.core.config import settings
from app.core.logging import bind_context, clear_context, configure_logging

logger = logging.getLogger(__name__)

celery_app = Celery(
    "complipilot",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.deadline_tasks",
        "app.tasks.notification_tasks",
        "app.tasks.document_tasks",
        "app.tasks.regulatory_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
    enable_utc=True,
    # Acknowledge after the task body returns, not when it is delivered. A
    # worker killed mid-parse should hand the document back to the queue rather
    # than lose it — every task here is written to be idempotent, which is what
    # makes late acknowledgement safe.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    # A task that has run for ten minutes is stuck. The soft limit gives it a
    # chance to raise and clean up; the hard limit thirty seconds later kills
    # it regardless.
    task_soft_time_limit=600,
    task_time_limit=630,
    result_expires=86400,
    task_always_eager=not settings.celery_enabled,
    task_eager_propagates=not settings.celery_enabled,
)

# All times are IST, per the timezone above. Indian statutory deadlines are IST
# dates and a sweep that ran at 05:30 IST because someone set UTC would send
# "1 day left" reminders on the wrong day around month boundaries.
celery_app.conf.beat_schedule = {
    # Hourly rather than daily: reminders are cheap, and an hourly sweep means
    # a filing marked done at noon stops chasing people the same afternoon
    # instead of the next morning.
    "sweep-deadline-reminders": {
        "task": "app.tasks.deadline_tasks.sweep_deadline_reminders",
        "schedule": crontab(minute=15),
    },
    # Once a day, early, so a newly generated period's filings exist before
    # anyone opens the dashboard.
    "generate-upcoming-filings": {
        "task": "app.tasks.deadline_tasks.generate_upcoming_filings",
        "schedule": crontab(hour=1, minute=0),
    },
    "escalate-overdue-filings": {
        "task": "app.tasks.deadline_tasks.escalate_overdue",
        "schedule": crontab(hour=9, minute=30),
    },
    # Regulators publish through the working day; twice daily catches a morning
    # circular before the evening and an evening one before the next morning.
    "analyse-regulatory-updates": {
        "task": "app.tasks.regulatory_tasks.analyse_pending_updates",
        "schedule": crontab(hour="8,18", minute=0),
    },
    "retry-failed-notifications": {
        "task": "app.tasks.notification_tasks.retry_failed",
        "schedule": crontab(minute="*/20"),
    },
    # The DPDP 72-hour clock is short enough that a daily check is too coarse.
    "check-breach-deadlines": {
        "task": "app.tasks.notification_tasks.check_breach_deadlines",
        "schedule": crontab(minute="*/30"),
    },
}


@task_prerun.connect
def _task_prerun(task_id=None, task=None, kwargs=None, **_):
    """Give every task's log lines the same shape a request's have.

    Without this a worker's logs cannot be correlated with the request that
    queued the work, which is the first thing anyone wants when a reminder did
    not go out.
    """
    configure_logging()
    fields = {"task_id": task_id, "task": getattr(task, "name", "unknown")}
    # Tasks take ``organization_id`` by convention precisely so this hook can
    # find it; a task that omits it simply logs without one.
    org_id = (kwargs or {}).get("organization_id")
    if org_id is not None:
        fields["org_id"] = org_id
    bind_context(**fields)


@task_postrun.connect
def _task_postrun(**_):
    clear_context()
