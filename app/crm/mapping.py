"""Map our lead and pipeline stage onto HubSpot contact and deal properties.

Stage ids are HubSpot's defaults for the built-in sales pipeline. A portal with a custom
pipeline would swap in its own ids here; nothing else changes.
"""

from __future__ import annotations

from app.models import Lead, LeadStage

DEAL_PIPELINE = "default"

DEAL_STAGE: dict[str, str] = {
    LeadStage.NEW: "appointmentscheduled",
    LeadStage.QUALIFIED: "qualifiedtobuy",
    LeadStage.CONTACTED: "presentationscheduled",
    LeadStage.WON: "closedwon",
    LeadStage.LOST: "closedlost",
}

LIFECYCLE_STAGE: dict[str, str] = {
    LeadStage.NEW: "lead",
    LeadStage.QUALIFIED: "salesqualifiedlead",
    LeadStage.CONTACTED: "opportunity",
    LeadStage.WON: "customer",
    LeadStage.LOST: "other",
}

LEAD_STATUS: dict[str, str] = {
    LeadStage.NEW: "NEW",
    LeadStage.QUALIFIED: "OPEN",
    LeadStage.CONTACTED: "CONNECTED",
    LeadStage.WON: "CONNECTED",
    LeadStage.LOST: "UNQUALIFIED",
}


def split_name(name: str) -> tuple[str, str]:
    parts = name.split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def contact_properties(lead: Lead) -> dict[str, str]:
    first, last = split_name(lead.name)
    props = {
        "firstname": first,
        "lastname": last,
        "lifecyclestage": LIFECYCLE_STAGE[lead.stage],
        "hs_lead_status": LEAD_STATUS[lead.stage],
        "lead_source": lead.source,
        "external_lead_id": str(lead.id),
    }
    if lead.email:
        props["email"] = lead.email
    if lead.phone:
        props["phone"] = lead.phone
    return props


def deal_properties(lead: Lead) -> dict[str, str]:
    props = {
        "dealname": f"{lead.name} - {lead.source}",
        "pipeline": DEAL_PIPELINE,
        "dealstage": DEAL_STAGE[lead.stage],
        "external_lead_id": str(lead.id),
    }
    if lead.score is not None:
        props["lead_score"] = str(lead.score)
    if lead.stage == LeadStage.LOST and lead.lost_reason:
        props["closed_lost_reason"] = lead.lost_reason
    return props
