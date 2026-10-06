"""Test IaC di base (Task 2.1) - CDK assertions sul template sintetizzato.

Copre (Requirements 9.5, 10.2, 10.4):
- Tag obbligatori (Project, Environment, Owner) presenti sulle risorse
  taggabili.
- Billing alarm a soglia 1 USD presente con notifica SNS.
- Flag `paid_components_enabled` a False di default (nessun componente a
  pagamento sintetizzato).

Tutti i test lavorano offline sul template: nessun deploy, nessun costo.
"""
import aws_cdk as cdk
from aws_cdk import assertions

from stacks.centralino_stack import CentralinoStack

# Tag obbligatori attesi (Requirement 9.5).
EXPECTED_TAGS = {
    "Project": "centralino-ai-studio-medico",
    "Owner": "savinoas",
    "Environment": "dev",
}

# Risorse taggabili prodotte dallo stack di base che devono portare i tag.
TAGGABLE_RESOURCE_TYPES = ["AWS::SNS::Topic", "AWS::CloudWatch::Alarm"]


def _synth_template(paid: bool = False, environment_name: str = "dev"):
    """Sintetizza lo stack e restituisce (template, stack) per le assertion."""
    app = cdk.App()
    stack = CentralinoStack(
        app,
        "TestCentralinoStack",
        environment_name=environment_name,
        paid_components_enabled=paid,
        env=cdk.Environment(region="us-east-1"),
    )
    return assertions.Template.from_stack(stack), stack


def _tags_as_dict(tag_list):
    """Converte la lista di tag CloudFormation [{Key,Value}] in dict."""
    return {t["Key"]: t["Value"] for t in tag_list}


# --- Requirement 9.5: tag obbligatori su tutte le risorse taggabili ---

def test_mandatory_tags_present_on_all_taggable_resources():
    template, _ = _synth_template()
    resources = template.to_json().get("Resources", {})

    taggable = {
        lid: res
        for lid, res in resources.items()
        if res.get("Type") in TAGGABLE_RESOURCE_TYPES
    }
    # Deve esserci almeno una risorsa taggabile (topic + alarm).
    assert taggable, "Nessuna risorsa taggabile trovata nel template"

    for logical_id, res in taggable.items():
        props = res.get("Properties", {})
        tags = _tags_as_dict(props.get("Tags", []))
        for key, value in EXPECTED_TAGS.items():
            assert tags.get(key) == value, (
                f"Risorsa {logical_id} ({res['Type']}) manca/errato tag "
                f"{key}={value}, trovato: {tags.get(key)!r}"
            )


def test_environment_tag_follows_environment_name():
    template, _ = _synth_template(environment_name="prod")
    resources = template.to_json().get("Resources", {})
    for res in resources.values():
        if res.get("Type") in TAGGABLE_RESOURCE_TYPES:
            tags = _tags_as_dict(res.get("Properties", {}).get("Tags", []))
            assert tags.get("Environment") == "prod"


def test_invalid_environment_name_rejected():
    import pytest

    app = cdk.App()
    with pytest.raises(ValueError):
        CentralinoStack(
            app,
            "BadEnvStack",
            environment_name="staging",  # non ammesso
            env=cdk.Environment(region="us-east-1"),
        )


# --- Requirement 10.4: billing alarm a 1 USD con notifica SNS ---

def test_billing_alarm_at_1_usd_present():
    template, _ = _synth_template()
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "AWS/Billing",
            "MetricName": "EstimatedCharges",
            "Threshold": 1,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        },
    )


def test_billing_alarm_notifies_sns_topic():
    template, _ = _synth_template()
    # Esattamente un topic SNS come canale di notifica.
    template.resource_count_is("AWS::SNS::Topic", 1)
    # L'allarme referenzia il topic come AlarmAction.
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "AlarmActions": assertions.Match.array_with(
                [{"Ref": assertions.Match.any_value()}]
            ),
        },
    )


# --- Requirement 10.2: paid_components_enabled default False ---

def test_paid_components_disabled_by_default():
    _, stack = _synth_template()  # default paid=False
    assert stack.paid_components_enabled is False


def test_no_paid_components_synthesized_by_default():
    """Nessuna risorsa Connect/Bedrock nel template di default (costo-zero)."""
    template, _ = _synth_template(paid=False)
    resources = template.to_json().get("Resources", {})
    types = {res.get("Type") for res in resources.values()}
    paid_types = {
        "AWS::Connect::Instance",
        "AWS::Connect::ContactFlow",
        "AWS::Lex::Bot",
        "AWS::Bedrock::Agent",
    }
    assert not (types & paid_types), (
        f"Trovate risorse a pagamento non attese: {types & paid_types}"
    )


def test_flag_can_be_enabled():
    """Il flag e' propagato quando abilitato (per gate future)."""
    _, stack = _synth_template(paid=True)
    assert stack.paid_components_enabled is True
