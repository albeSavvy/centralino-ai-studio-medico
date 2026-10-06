"""Test IaC (Task 16) - CDK assertions sullo strato voce Lex V2.

Lex e' un componente A PAGAMENTO (Requirement 10.2), quindi e' gated dal flag
`paid_components_enabled`. Questi test proteggono due garanzie:

- COSTO-ZERO (Requirement 10.1, 10.2): con paid_components_enabled=False
  (default) la synth NON produce ALCUNA risorsa Lex (bot, alias, ruolo Lex,
  permission Lex). E' il comportamento di default: nessun costo vocale.
- WIRING (Requirement 1.2, 2.1): con paid_components_enabled=True viene
  sintetizzato un bot Lex V2 in italiano con un intent catch-all
  (AMAZON.FallbackIntent + fulfillment code hook) e un alias che associa il
  code hook alla Lambda motore-conversazionale, con permesso di invoke a Lex.

Tutti i test lavorano offline sul template sintetizzato: nessun deploy, nessun
costo (coerente col guardrail costo-zero della Fase 6).
"""
import aws_cdk as cdk
from aws_cdk import assertions

from stacks.centralino_stack import CentralinoStack

BOT_TYPE = "AWS::Lex::Bot"
ALIAS_TYPE = "AWS::Lex::BotAlias"
EXPECTED_LOCALE = "it_IT"
FALLBACK_SIGNATURE = "AMAZON.FallbackIntent"

# Tipi di risorsa Lex che NON devono comparire quando i componenti a pagamento
# sono disattivati (default).
LEX_RESOURCE_TYPES = {
    "AWS::Lex::Bot",
    "AWS::Lex::BotAlias",
    "AWS::Lex::BotVersion",
}


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


def _resources_of_type(template, res_type):
    resources = template.to_json().get("Resources", {})
    return {
        lid: res
        for lid, res in resources.items()
        if res.get("Type") == res_type
    }


# --- Requirement 10.2: default (flag=false) => NESSUNA risorsa Lex ---

def test_no_lex_resources_when_paid_disabled_default():
    """Costo-zero: la synth di default non crea alcuna risorsa Lex."""
    template, _ = _synth_template(paid=False)
    resources = template.to_json().get("Resources", {})
    types = {res.get("Type") for res in resources.values()}
    found = types & LEX_RESOURCE_TYPES
    assert not found, f"Trovate risorse Lex non attese col flag off: {found}"


def test_lex_bot_count_zero_by_default():
    template, _ = _synth_template(paid=False)
    template.resource_count_is(BOT_TYPE, 0)
    template.resource_count_is(ALIAS_TYPE, 0)


def test_lex_attributes_none_by_default():
    """Con il flag off lo stack non espone alcun bot Lex."""
    _, stack = _synth_template(paid=False)
    assert stack.lex_bot is None
    assert stack.lex_bot_alias is None
    assert stack.lex_role is None


def test_no_lex_iam_role_when_paid_disabled():
    """Nessun ruolo di servizio Lex nel template di default."""
    template, _ = _synth_template(paid=False)
    roles = _resources_of_type(template, "AWS::IAM::Role")
    for lid, res in roles.items():
        statements = (
            res.get("Properties", {})
            .get("AssumeRolePolicyDocument", {})
            .get("Statement", [])
        )
        principals = [
            s.get("Principal", {}).get("Service") for s in statements
        ]
        assert "lexv2.amazonaws.com" not in principals, (
            f"Ruolo Lex {lid} non atteso col flag off"
        )


# --- Requirement 1.2, 2.1: flag=true => bot catch-all -> Lambda ---

def test_lex_bot_synthesized_when_paid_enabled():
    template, _ = _synth_template(paid=True)
    template.resource_count_is(BOT_TYPE, 1)
    template.resource_count_is(ALIAS_TYPE, 1)


def test_lex_bot_is_italian_locale():
    template, _ = _synth_template(paid=True)
    template.has_resource_properties(
        BOT_TYPE,
        {
            "BotLocales": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {"LocaleId": EXPECTED_LOCALE}
                    )
                ]
            )
        },
    )


def test_lex_bot_has_catch_all_fallback_intent_with_fulfillment():
    """L'intent catch-all e' la FallbackIntent con fulfillment code hook on."""
    template, _ = _synth_template(paid=True)
    template.has_resource_properties(
        BOT_TYPE,
        {
            "BotLocales": assertions.Match.array_with(
                [
                    assertions.Match.object_like(
                        {
                            "LocaleId": EXPECTED_LOCALE,
                            "Intents": assertions.Match.array_with(
                                [
                                    assertions.Match.object_like(
                                        {
                                            "ParentIntentSignature": (
                                                FALLBACK_SIGNATURE
                                            ),
                                            "FulfillmentCodeHook": {
                                                "Enabled": True
                                            },
                                        }
                                    )
                                ]
                            ),
                        }
                    )
                ]
            )
        },
    )


def test_lex_bot_uses_dedicated_lex_role():
    """Il bot ha un ruolo di servizio con principal lexv2.amazonaws.com."""
    template, _ = _synth_template(paid=True)
    # Il bot referenzia un ruolo via Fn::GetAtt.
    template.has_resource_properties(
        BOT_TYPE,
        {"RoleArn": {"Fn::GetAtt": assertions.Match.any_value()}},
    )
    # Esiste un ruolo assumibile da Lex V2.
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Principal": {
                                    "Service": "lexv2.amazonaws.com"
                                }
                            }
                        )
                    ]
                )
            }
        },
    )


def test_lex_alias_wires_code_hook_to_motore_lambda():
    """L'alias associa il LambdaCodeHook alla Lambda motore-conversazionale."""
    template, stack = _synth_template(paid=True)
    aliases = _resources_of_type(template, ALIAS_TYPE)
    assert len(aliases) == 1
    _, alias = next(iter(aliases.items()))
    settings = alias["Properties"]["BotAliasLocaleSettings"]
    # Almeno un locale settings con code hook Lambda per l'italiano.
    hooks = []
    for item in settings:
        assert item["LocaleId"] == EXPECTED_LOCALE
        setting = item["BotAliasLocaleSetting"]
        assert setting["Enabled"] is True
        lambda_hook = setting["CodeHookSpecification"]["LambdaCodeHook"]
        hooks.append(lambda_hook)
    assert hooks, "Nessun LambdaCodeHook nell'alias Lex"
    for hook in hooks:
        assert hook["CodeHookInterfaceVersion"] == "1.0"
        # L'ARN punta alla Lambda motore (Fn::GetAtt su quella funzione).
        arn = hook["LambdaArn"]
        assert isinstance(arn, dict) and "Fn::GetAtt" in arn, (
            "Il code hook deve puntare alla Lambda motore via Fn::GetAtt"
        )


def test_lex_can_invoke_motore_lambda():
    """Esiste una Lambda permission che consente a Lex di invocare la Lambda."""
    template, _ = _synth_template(paid=True)
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunction",
            "Principal": "lexv2.amazonaws.com",
        },
    )


def test_lex_attributes_exposed_when_paid_enabled():
    _, stack = _synth_template(paid=True)
    assert stack.lex_bot is not None
    assert stack.lex_bot_alias is not None
    assert stack.lex_role is not None


# --- Il bot Lex porta i tag obbligatori (Requirement 9.5) ---

def test_lex_bot_carries_mandatory_tags():
    """Lex usa BotTags per i tag; devono esserci i 3 tag obbligatori."""
    template, _ = _synth_template(paid=True)
    bots = _resources_of_type(template, BOT_TYPE)
    _, bot = next(iter(bots.items()))
    tags = {
        t["Key"]: t["Value"]
        for t in bot.get("Properties", {}).get("BotTags", [])
    }
    assert tags.get("Project") == "centralino-ai-studio-medico"
    assert tags.get("Owner") == "savinoas"
    assert tags.get("Environment") == "dev"
