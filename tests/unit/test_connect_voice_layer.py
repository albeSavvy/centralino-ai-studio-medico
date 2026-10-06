"""Test IaC (Task 17) - CDK assertions sul contact flow Amazon Connect.

Amazon Connect e' un componente A PAGAMENTO (numero telefonico + tariffa al
minuto, Requirement 10.2), quindi e' gated dal flag `paid_components_enabled`.
Questi test proteggono due garanzie:

- COSTO-ZERO (Requirement 10.1, 10.2): con paid_components_enabled=False
  (default) la synth NON produce ALCUNA risorsa Connect (istanza, contact flow,
  associazione Lex). E' il comportamento di default: nessun costo telefonico.
- WIRING (Requirement 1.1-1.5): con paid_components_enabled=True viene
  sintetizzata un'istanza Connect con inbound abilitato, un contact flow inbound
  in italiano che delega ASR/TTS al bot Lex V2 (Task 16) e ne gestisce ri-prompt
  e bassa confidenza, piu' l'associazione del bot Lex all'istanza.

Tutti i test lavorano offline sul template sintetizzato: nessun deploy, nessun
bootstrap, nessun costo (coerente col guardrail costo-zero della Fase 6).

Commenti in ASCII.
"""
import json

import aws_cdk as cdk
from aws_cdk import assertions

from stacks.centralino_stack import CentralinoStack


def _flow_content(flow) -> dict:
    """Ritorna il Content del contact flow come dict.

    Il Content e' una stringa JSON che pero' embedda l'ARN dell'alias del bot Lex
    (un token CloudFormation): CDK la rappresenta quindi come `Fn::Join` di parti,
    dove le parti-stringa sono i frammenti di JSON e le parti-dict sono i
    riferimenti al token (es. {"Fn::GetAtt": [...]}). Ricostruiamo la stringa
    sostituendo ogni token con un placeholder, poi facciamo il parse JSON.
    Gestiamo anche il caso in cui il Content sia gia' stringa o dict semplice.
    """
    content = flow["Properties"]["Content"]
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, dict) and "Fn::Join" in content:
        _, parts = content["Fn::Join"]
        rebuilt = "".join(
            p if isinstance(p, str) else "TOKEN" for p in parts
        )
        return json.loads(rebuilt)
    return content

INSTANCE_TYPE = "AWS::Connect::Instance"
FLOW_TYPE = "AWS::Connect::ContactFlow"
ASSOCIATION_TYPE = "AWS::Connect::IntegrationAssociation"

# Tipi di risorsa Connect che NON devono comparire quando i componenti a
# pagamento sono disattivati (default).
CONNECT_RESOURCE_TYPES = {
    "AWS::Connect::Instance",
    "AWS::Connect::ContactFlow",
    "AWS::Connect::ContactFlowModule",
    "AWS::Connect::IntegrationAssociation",
    "AWS::Connect::PhoneNumber",
}


def _synth_template(paid: bool = False, environment_name: str = "dev"):
    """Sintetizza lo stack e restituisce (template, stack) per le assertion."""
    app = cdk.App()
    stack = CentralinoStack(
        app,
        "TestConnectStack",
        environment_name=environment_name,
        paid_components_enabled=paid,
        env=cdk.Environment(region="us-east-1"),
    )
    return assertions.Template.from_stack(stack), stack


def _resources_of_type(template, res_type):
    resources = template.to_json().get("Resources", {})
    return {
        lid: res
        for lid, res in resources.items()
        if res.get("Type") == res_type
    }


# --- Requirement 10.2: default (flag=false) => NESSUNA risorsa Connect ---

def test_no_connect_resources_when_paid_disabled_default():
    """Costo-zero: la synth di default non crea alcuna risorsa Connect."""
    template, _ = _synth_template(paid=False)
    resources = template.to_json().get("Resources", {})
    types = {res.get("Type") for res in resources.values()}
    found = types & CONNECT_RESOURCE_TYPES
    assert not found, f"Trovate risorse Connect non attese col flag off: {found}"


def test_connect_counts_zero_by_default():
    template, _ = _synth_template(paid=False)
    template.resource_count_is(INSTANCE_TYPE, 0)
    template.resource_count_is(FLOW_TYPE, 0)
    template.resource_count_is(ASSOCIATION_TYPE, 0)


def test_connect_attributes_none_by_default():
    """Con il flag off lo stack non espone alcuna risorsa Connect."""
    _, stack = _synth_template(paid=False)
    assert stack.connect_instance is None
    assert stack.connect_contact_flow is None
    assert stack.connect_lex_association is None


# --- Requirement 1.1-1.5: flag=true => istanza + contact flow inbound -> Lex ---

def test_connect_synthesized_when_paid_enabled():
    template, _ = _synth_template(paid=True)
    template.resource_count_is(INSTANCE_TYPE, 1)
    template.resource_count_is(FLOW_TYPE, 1)
    template.resource_count_is(ASSOCIATION_TYPE, 1)


def test_connect_instance_inbound_enabled():
    """L'istanza Connect abilita le chiamate inbound (Requirement 1.1)."""
    template, _ = _synth_template(paid=True)
    template.has_resource_properties(
        INSTANCE_TYPE,
        {
            "IdentityManagementType": "CONNECT_MANAGED",
            "Attributes": assertions.Match.object_like({"InboundCalls": True}),
        },
    )


def test_connect_flow_is_inbound_contact_flow():
    """Il contact flow e' di tipo CONTACT_FLOW (flusso inbound)."""
    template, _ = _synth_template(paid=True)
    template.has_resource_properties(
        FLOW_TYPE,
        {"Type": "CONTACT_FLOW"},
    )


def test_connect_flow_content_has_italian_welcome_and_reprompt():
    """Il contenuto del flusso ha benvenuto IT, ri-prompt e bassa confidenza."""
    template, _ = _synth_template(paid=True)
    flows = _resources_of_type(template, FLOW_TYPE)
    assert len(flows) == 1
    _, flow = next(iter(flows.items()))
    content = _flow_content(flow)

    identifiers = {a["Identifier"] for a in content["Actions"]}
    # Benvenuto (1.1), ri-prompt su silenzio (1.4), bassa confidenza (1.5),
    # chiusura (1.4) e disconnessione sono tutti presenti nel flusso.
    assert {"welcome", "reprompt", "lowConfidence", "goodbye", "disconnect"} <= identifiers

    # Lo start action e' il benvenuto (Requirement 1.1: prima cosa alla chiamata).
    assert content["StartAction"] == "welcome"

    # Il testo del benvenuto e' in italiano e cita la sede di Meda.
    welcome = next(a for a in content["Actions"] if a["Identifier"] == "welcome")
    assert "Meda" in welcome["Parameters"]["Text"]


def test_connect_flow_delegates_asr_to_lex_bot():
    """La raccolta input delega al bot Lex V2 (ASR/TTS), con barge-in on."""
    template, _ = _synth_template(paid=True)
    flows = _resources_of_type(template, FLOW_TYPE)
    _, flow = next(iter(flows.items()))
    content = _flow_content(flow)

    lex_block = next(
        a for a in content["Actions"]
        if a["Type"] == "ConnectParticipantWithLexBot"
    )
    params = lex_block["Parameters"]
    assert params["BargeInEnabled"] == "true"
    # Timeout input di 10s (Requirement 1.4).
    assert params["InputTimeLimitSeconds"] == "10"
    # Referenzia un alias di bot Lex (AliasArn presente).
    assert "AliasArn" in params["LexV2Bot"]


def test_connect_associates_lex_bot_to_instance():
    """Esiste un'associazione LEX_BOT tra l'istanza Connect e il bot Lex."""
    template, _ = _synth_template(paid=True)
    template.has_resource_properties(
        ASSOCIATION_TYPE,
        {"IntegrationType": "LEX_BOT"},
    )


def test_connect_requires_lex_layer_present():
    """Con Connect attivo esiste anche il bot Lex (Connect delega a Lex)."""
    template, _ = _synth_template(paid=True)
    template.resource_count_is("AWS::Lex::Bot", 1)


def test_connect_attributes_exposed_when_paid_enabled():
    _, stack = _synth_template(paid=True)
    assert stack.connect_instance is not None
    assert stack.connect_contact_flow is not None
    assert stack.connect_lex_association is not None


# --- Il contact flow porta i tag obbligatori (Requirement 9.5) ---

def test_connect_instance_carries_mandatory_tags():
    """L'istanza Connect porta i 3 tag obbligatori (propagati da Tags.of)."""
    template, _ = _synth_template(paid=True)
    instances = _resources_of_type(template, INSTANCE_TYPE)
    _, instance = next(iter(instances.items()))
    tags = {
        t["Key"]: t["Value"]
        for t in instance.get("Properties", {}).get("Tags", [])
    }
    assert tags.get("Project") == "centralino-ai-studio-medico"
    assert tags.get("Owner") == "savinoas"
    assert tags.get("Environment") == "dev"
