# Argos L1 Support Alignment & Gap Assessment

Date: March 11, 2026  
Codebase: `/home/aa/Desktop/WORK/ARCGEN/Argos`

## 1. Executive Summary

Argos has a strong foundation for real-time CloudWatch alarm triage and basic automated remediation.  
It is partially aligned with the client’s L1 incident-response needs, but significant gaps remain in escalation orchestration, governance reporting, WAFR automation, security operations, and monitoring-noise optimization.

Estimated current alignment: **30-40%** against the 7 high-level client requirements.

Update (March 11, 2026):
- F2 (event normalization), F3 (dedup failure semantics), and F4 (ALARM-state filtering) have been implemented in code and validated.
- Added incident-report signaling for missing/deleted resources plus CloudTrail event lookup support for investigation.

## 2. Scope and Review Method

Reviewed:
- Runtime entrypoints, graph orchestration, and node behaviors
- AWS/Jira/Telegram/SNS integrations
- Infrastructure template and monitoring setup
- Test/debug scripts and dependency setup

Primary files reviewed include:
- `main.py`
- `agents/alert_triage/graph.py`
- `agents/alert_triage/nodes/*.py`
- `tools/*.py`
- `utils/deduplication.py`
- `infra/ec2-monitoring-stack.yaml`
- `README.md`

## 3. Requirement-by-Requirement Alignment

### R1. Alert intake, investigation, remediation, ticket and comms updates
**Status: Partially Aligned**

What exists:
- Ingests CloudWatch alarm events from webhook and runs autonomous triage graph
- Investigation agent checks metrics/logs/service health
- Remediation agent executes safe actions (redeploy/reboot/scale-out)
- Creates Jira incident, posts Telegram alert, sends SMS for P1

Key evidence:
- `main.py` (`/alert-webhook`, background triage)
- `agents/alert_triage/graph.py` (ingest -> classify -> investigate -> remediate -> notify)
- `agents/alert_triage/nodes/investigate_agent.py`
- `agents/alert_triage/nodes/remediate_agent.py`
- `agents/alert_triage/nodes/notify.py`

Gaps:
- No Slack or Microsoft Teams integration
- Jira issue update flow exists but not used in graph lifecycle

---

### R2. P1 coordination with on-call and escalation matrix
**Status: Low Alignment**

What exists:
- Sends one SMS to one configured phone for P1 alerts

Key evidence:
- `agents/alert_triage/nodes/notify.py` (`_send_sms_if_p1`)
- `tools/sns_notify.py` (`SNS_ONCALL_PHONE` single target)

Gaps:
- No escalation tiers/matrix
- No acknowledgement tracking
- No timed retries/failover to next on-call
- No call workflow integration

---

### R3. Weekly/monthly capacity monitoring and reporting
**Status: Not Aligned**

What exists:
- Incident-time metric checks only

Gaps:
- No scheduled capacity jobs
- No weekly/monthly reports
- No trend storage or forecasting logic

---

### R4. Cost optimization and analysis reporting
**Status: Not Aligned**

What exists:
- No cost analytics pipeline in current codebase

Gaps:
- No Cost Explorer/Budgets ingestion
- No optimization recommendation engine
- No report generation/distribution workflow

---

### R5. Dedicated WAFR-focused agent(s)
**Status: Not Aligned**

What exists:
- No WAFR domain workflows, checklists, scoring, or recommendation engine

Gaps:
- No Well-Architected pillar-specific analysis
- No engagement-level WAFR report automation

---

### R6. Security review and optimization
**Status: Low Alignment**

What exists:
- Safety guardrails around allowed AWS write actions

Key evidence:
- `tools/aws_boto.py` (whitelist model)

Gaps:
- No Security Hub/GuardDuty/Inspector posture analysis
- No recurring security review workflow
- No remediation recommendation/reporting for security controls

---

### R7. Alert-noise detection, threshold tuning, and monitoring coverage checks
**Status: Low Alignment**

What exists:
- Basic dedup and flapping cooldown suppression

Key evidence:
- `utils/deduplication.py`

Gaps:
- No false-positive/noise analytics
- No threshold tuning recommendations
- No “required monitoring coverage by tech stack” validation
- No monitoring maturity score/report

## 4. Critical and High-Risk Findings

### F1. Unauthenticated inbound control paths (Critical)
- `POST /alert-webhook` and `POST /telegram-webhook` accept requests without robust request verification.
- These paths can trigger agent-driven AWS operations.

Evidence:
- `main.py` routes and handler logic

Impact:
- Security exposure and unauthorized action risk.

---

### F2. SQS-wrapped payload handling inconsistency (Critical) - Mitigated
- Implemented normalized event extraction before triage, supporting:
  - Direct EventBridge payload
  - SQS body containing EventBridge payload
  - SNS-in-SQS wrapper (`Message` containing EventBridge payload)
- Triage now always receives normalized CloudWatch event shape.

Evidence:
- `main.py` (`_extract_cloudwatch_alarm_event`, `_run_triage`)

Impact:
- Risk significantly reduced for wrapper-related misprocessing.

---

### F3. Dedup flapping suppression after failed triage (High) - Mitigated
- Dedup now claims only exact-event key during processing.
- Flapping cooldown key is written only after successful triage completion.

Evidence:
- `utils/deduplication.py` (`mark_processing`, `mark_completed`)

Impact:
- Significantly lowers risk of suppressing valid post-failure alerts.

---

### F4. Alert-state filtering is incomplete at app layer (High) - Mitigated
- Webhook now filters to process only `detail.state.value == "ALARM"`.
- Non-ALARM CloudWatch state change events are ignored.

Evidence:
- `main.py` (`_is_cloudwatch_alarm_event`)

Impact:
- Reduced noise and unnecessary triage execution.

## 5. Architecture and Documentation Drift

- Runtime graph uses `investigate_agent` and `remediate_agent` as active nodes.
- Legacy nodes (`gather_evidence.py`, `analyze.py`, `remediate.py`) still exist and are heavily referenced in README.
- This creates onboarding/maintenance confusion.

Evidence:
- `agents/alert_triage/graph.py` vs `README.md` sections referencing old flow.

## 6. Prioritized Improvement Roadmap

### Phase 1: Stabilize and Secure (Immediate)
1. Add strong webhook authentication/verification and enforce request trust boundaries.
2. Normalize inbound event format once (direct + SQS) before dedup/classify. **(Completed)**
3. Ensure ALARM-state filtering before invoking triage. **(Completed)**
4. Fix dedup failure semantics to avoid suppressing valid post-failure alerts. **(Completed)**
5. Remove payload `print()` and tighten sensitive logging controls. **(Completed, payload print removed)**

### Phase 2: P1 Operations Readiness
1. Implement escalation matrix engine (tiered contacts, retry intervals, next-tier failover).
2. Add acknowledgement tracking and escalation state persistence.
3. Add Slack/Teams integrations with structured incident updates.
4. Add Jira lifecycle updates (created, remediation attempts, resolved/escalated).

### Phase 3: Reporting and Optimization
1. Build scheduled capacity reports (weekly/monthly) with trend baselines.
2. Build cost analysis/optimization report pipeline.
3. Add alert-noise analytics and threshold-tuning recommendations.
4. Add monitoring coverage audit against detected stack components.

### Phase 4: Strategic Agent Capabilities
1. Build dedicated WAFR agent/workflow (pillar checks, findings, action plan).
2. Build security review workflow (posture findings + optimization recommendations).
3. Add governance dashboards and recurring executive report outputs.

## 7. Recommended Delivery Model

Implement as modular workflows:
- **Incident Response Agent** (existing, hardened)
- **Escalation Orchestrator Agent** (new)
- **Capacity & Cost Reporting Agent** (new, scheduled)
- **WAFR Agent** (new, engagement-focused)
- **Security Review Agent** (new, posture-focused)
- **Monitoring Quality Agent** (new, noise + coverage)

This preserves the current core while adding the missing enterprise operations capabilities.

## 8. Final Assessment

Argos is a solid incident-triage foundation, but not yet a full L1 support operations platform as requested by the client.  
The fastest path is to harden current runtime security/reliability first, then add escalation, reporting, and governance-focused agents in phased releases.

## 9. Verification Evidence (March 11, 2026)

Executed checks:
- `python3 -m py_compile $(rg --files -g '*.py')` -> passed
- Event normalization + ALARM filtering test script (direct, SQS, SNS-in-SQS, invalid payload) -> `main_event_tests_passed`
- Dedup semantics test script (mocked writes) -> `dedup_semantics_tests_passed`

Validated outcomes:
- Non-ALARM state events are ignored at app layer.
- SQS/SNS wrapped events are normalized before dedup/classify/triage.
- Flap cooldown is set only after successful triage completion.

## 10. Missing/Deleted Resource Handling (March 11, 2026)

Implemented enhancements:
- Remediation outputs now preserve AWS error details (`error_code`, `error`) and flag `resource_missing_or_deleted=true` when detected.
- Final incident report now includes:
  - `resource_missing_or_deleted` (boolean)
  - `resource_issue_evidence` (evidence list)
- Telegram incident update includes a **Resource Status Note** when this condition is detected.
- Investigation agent now has `lookup_resource_change_events` (CloudTrail) to check who/what changed/deleted a resource.

Operational answer:
- Yes, it is possible to check logs/history of what happened to the resource.
- This is done through CloudTrail management events (e.g., delete/terminate/stop actions).
