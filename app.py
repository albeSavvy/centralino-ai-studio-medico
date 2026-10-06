#!/usr/bin/env python3
"""Entry point della CDK App - Centralino AI Studio Medico (Idelia).

Una "App" e' il contenitore di piu' alto livello. Dentro ci mettiamo
uno o piu' "Stack". Ogni Stack corrisponde a uno stack CloudFormation.

Task 2: istanzia lo stack principale (CentralinoStack) con:
- tag obbligatori (Project, Environment, Owner=savinoas),
- billing alarm a 1 USD con notifica SNS,
- flag `paid_components_enabled` (default False) per gate-are i componenti a
  pagamento (Connect, Bedrock reale) nelle task successive.

Costo-zero: qui si sintetizza solo il template. Nessun deploy/bootstrap.
"""
import aws_cdk as cdk

from stacks.centralino_stack import CentralinoStack

app = cdk.App()

# --- Flag costo-zero (Requirement 10.2) ---
# Letto dal context CDK (cdk.json o -c paid_components_enabled=...). Default
# False: i componenti a pagamento (Amazon Connect, Amazon Bedrock reale)
# restano disabilitati. Accetta sia il booleano JSON sia la stringa "true".
_paid_ctx = app.node.try_get_context("paid_components_enabled")
paid_components_enabled = str(_paid_ctx).lower() == "true"

# --- Environment name per il tag Environment (Requirement 9.5) ---
# Selezionabile da context (-c environment=dev|test|prod); default "dev".
environment_name = app.node.try_get_context("environment") or "dev"

# La metrica di billing AWS/Billing EstimatedCharges e' disponibile solo in
# us-east-1: pinniamo la region dello stack a us-east-1 cosi' l'allarme di
# costo puo' funzionare. L'account resta risolto dall'ambiente CLI a deploy.
CentralinoStack(
    app,
    "CentralinoStack",
    environment_name=environment_name,
    paid_components_enabled=paid_components_enabled,
    env=cdk.Environment(region="us-east-1"),
)

# synth() traduce il codice Python nel template CloudFormation (JSON).
app.synth()
