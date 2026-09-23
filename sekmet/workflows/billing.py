"""Billing: Coverage, Account, ChargeItem, Claim submission and (simulated payer) adjudication."""
from __future__ import annotations

from ..fhir.common import codeable, instant, ref
from . import catalog as C
from .registry import Param, WF, workflow
from .target import WorkflowError


@workflow("billing.coverage", "Register insurance coverage", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("member_id", "Member id (blank = generated)"),
    Param("plan", "Plan", default="GOLD-PPO"),
])
def coverage(wf: WF, patient, member_id=None, plan="GOLD-PPO"):
    p = wf.get("Patient", patient)
    member = member_id or wf.number("M")
    cov = {
        "resourceType": "Coverage",
        "identifier": [wf.ident(wf.ids.member, member, "MB")],
        "status": "active",
        "type": codeable("http://terminology.hl7.org/CodeSystem/v3-ActCode", "EHCPOL", "extended healthcare"),
        "subscriber": ref(p),
        "subscriberId": member,
        "beneficiary": ref(p),
        "relationship": codeable("http://terminology.hl7.org/CodeSystem/subscriber-relationship", "self", "Self"),
        "period": {"start": instant()[:10]},
        "payor": [ref(wf.org("payer"))],
        "class": [{"type": codeable(C.COVERAGE_CLASS, "plan", "Plan"), "value": plan, "name": f"{plan} plan"}],
    }
    cov = wf.t.create(cov, if_none_exist=f"identifier={wf.ids.member}|{member}")
    return {"coverage": cov, "patient": p}


@workflow("billing.account", "Open patient account", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("coverage", "Coverage", "ref", ref_type="Coverage"),
])
def account(wf: WF, patient, coverage=None):
    p = wf.get("Patient", patient)
    acc = {
        "resourceType": "Account",
        "identifier": [wf.ident(wf.ids.generic, wf.number("ACCT"))],
        "status": "active",
        "type": codeable("http://terminology.hl7.org/CodeSystem/v3-ActCode", "PBILLACCT", "patient billing account"),
        "name": f"Account for {p.get('name', [{}])[0].get('text', p['id'])}",
        "subject": [ref(p)],
        "servicePeriod": {"start": instant()},
        "owner": ref(wf.org("hospital")),
    }
    if coverage:
        acc["coverage"] = [{"coverage": ref(wf.get("Coverage", coverage)), "priority": 1}]
    acc = wf.t.create(acc)
    return {"account": acc}


@workflow("billing.charge", "Post charge (ChargeItem, P03)", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
    Param("account", "Account", "ref", ref_type="Account"),
    Param("code", "Charge code", "choice", "CONSULT", list(C.CHARGES)),
    Param("quantity", "Quantity", "int", 1),
])
def charge(wf: WF, patient, encounter=None, account=None, code="CONSULT", quantity=1):
    p = wf.get("Patient", patient)
    display, price = C.CHARGES[code]
    ci = {
        "resourceType": "ChargeItem",
        "identifier": [wf.ident(wf.ids.generic, wf.number("CHG"))],
        "status": "billable",
        "code": codeable(C.CHARGE_CODES, code, display),
        "subject": ref(p),
        "occurrenceDateTime": instant(),
        "performer": [{"actor": ref(wf.practitioner("attending"))}],
        "performingOrganization": ref(wf.org("hospital")),
        "quantity": {"value": quantity},
        "priceOverride": {"value": price, "currency": "USD"},
        "enteredDate": instant(),
        "enterer": ref(wf.practitioner("attending")),
    }
    if encounter:
        ci["context"] = ref(wf.get("Encounter", encounter))
    if account:
        ci["account"] = [ref(wf.get("Account", account))]
    ci = wf.t.create(ci)
    wf.emit("charge", [ci], [p])
    return {"charge_item": ci}


@workflow("billing.claim", "Submit claim", [
    Param("patient", "Patient", "ref", required=True, ref_type="Patient"),
    Param("coverage", "Coverage", "ref", required=True, ref_type="Coverage"),
    Param("encounter", "Encounter", "ref", ref_type="Encounter"),
    Param("codes", "Charge codes (comma separated)", default="CONSULT,LAB-BMP"),
    Param("use_submit", "Use Claim/$submit operation", "bool", False),
])
def claim(wf: WF, patient, coverage, encounter=None, codes="CONSULT,LAB-BMP", use_submit=False):
    p = wf.get("Patient", patient)
    cov = wf.get("Coverage", coverage)
    enc = wf.get("Encounter", encounter) if encounter else None
    items, total = [], 0.0
    for seq, code in enumerate([c.strip() for c in codes.split(",") if c.strip()], start=1):
        if code not in C.CHARGES:
            raise WorkflowError(f"Unknown charge code {code}")
        display, price = C.CHARGES[code]
        item = {"sequence": seq, "productOrService": codeable(C.CHARGE_CODES, code, display),
                "servicedDate": instant()[:10], "quantity": {"value": 1},
                "unitPrice": {"value": price, "currency": "USD"}, "net": {"value": price, "currency": "USD"}}
        if enc:
            item["encounter"] = [ref(enc)]
        items.append(item)
        total += price
    cl = {
        "resourceType": "Claim",
        "identifier": [wf.ident(wf.ids.claim, wf.number("CLM"))],
        "status": "active",
        "type": codeable(C.CLAIM_TYPE, "institutional" if enc else "professional"),
        "use": "claim",
        "patient": ref(p),
        "created": instant(),
        "insurer": cov["payor"][0],
        "provider": ref(wf.org("hospital")),
        "priority": codeable(C.PROCESS_PRIORITY, "normal"),
        "payee": {"type": codeable(C.PAYEE_TYPE, "provider")},
        "insurance": [{"sequence": 1, "focal": True, "coverage": ref(cov)}],
        "item": items,
        "total": {"value": round(total, 2), "currency": "USD"},
    }
    if use_submit:
        client = getattr(wf.t, "client", None)
        if client is None:  # local target: call our own operation
            return {"claim_response": wf.ctx.service.operation("Claim", None, "$submit", [], cl).body}
        r = client.operation("Claim/$submit", body=cl)
        if not r.ok:
            raise WorkflowError(f"Claim/$submit failed: HTTP {r.status} {r.outcome_text()}")
        return {"claim_response": r.resource}
    cl = wf.t.create(cl)
    wf.emit("claim", [cl], [p, cov])
    return {"claim": cl}


def adjudicate_resource(claim: dict, insurer_ref: dict | None = None, deny: bool = False) -> dict:
    """Build a ClaimResponse approving 80% of each item (simulated payer)."""
    items, paid = [], 0.0
    for it in claim.get("item", []):
        net = (it.get("net") or it.get("unitPrice") or {}).get("value", 0)
        benefit = 0.0 if deny else round(net * 0.8, 2)
        paid += benefit
        items.append({"itemSequence": it.get("sequence", 1), "adjudication": [
            {"category": codeable(C.ADJUDICATION, "submitted"), "amount": {"value": net, "currency": "USD"}},
            {"category": codeable(C.ADJUDICATION, "eligible"), "amount": {"value": net, "currency": "USD"}},
            {"category": codeable(C.ADJUDICATION, "benefit"), "amount": {"value": benefit, "currency": "USD"}},
        ]})
    resp = {
        "resourceType": "ClaimResponse",
        "status": "active",
        "type": claim.get("type"),
        "use": claim.get("use", "claim"),
        "patient": claim.get("patient"),
        "created": instant(),
        "insurer": insurer_ref or claim.get("insurer"),
        "requestor": claim.get("provider"),
        "outcome": "complete" if not deny else "error",
        "disposition": "Claim settled as per contract." if not deny else "Claim denied.",
        "item": items,
        "total": [{"category": codeable(C.ADJUDICATION, "benefit"), "amount": {"value": round(paid, 2), "currency": "USD"}}],
        "payment": {"type": codeable("http://terminology.hl7.org/CodeSystem/ex-paymenttype", "complete"),
                    "date": instant()[:10], "amount": {"value": round(paid, 2), "currency": "USD"}},
    }
    if claim.get("id"):
        resp["request"] = {"reference": f"Claim/{claim['id']}"}
    return {k: v for k, v in resp.items() if v is not None}


@workflow("billing.adjudicate", "Adjudicate claim (payer side, ClaimResponse)", [
    Param("claim", "Claim", "ref", required=True, ref_type="Claim"),
    Param("deny", "Deny", "bool", False),
])
def adjudicate(wf: WF, claim, deny=False):
    cl = wf.get("Claim", claim)
    cr = adjudicate_resource(cl, deny=deny)
    cr = wf.t.create(cr)
    return {"claim_response": cr, "claim": cl}

