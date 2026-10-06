# Setup manuale — Idelia / Centralino AI Studio di Psicologia

> Guida passo-passo per configurare e testare il sistema, ordinata dal meno
> impegnativo (costo-zero, subito) al più (voce, a pagamento). L'implementazione
> CDK è completa e testata (190 test verdi, `cdk synth` OK); qui ci sono solo i
> passaggi MANUALI (account esterni, credenziali, console AWS, tuoi file) che il
> codice non può fare da solo.
>
> **Regola costo-zero:** ogni componente a pagamento (Bedrock reale, Connect) va
> attivato solo con conferma esplicita, billing alarm già attivo (soglia 1 USD),
> volume di test minimo, e `cdk destroy` a fine sessione.

## Ordine consigliato

Fai la **Fase A** per prima: è costo-zero e serve solo il tuo account AWS. Le fasi
successive aggiungono un pezzo alla volta. La voce (Connect) è l'ultima, a pagamento.

---

## FASE A — Test a costo zero (nessun servizio esterno)

Obiettivo: vedere Idelia dialogare via testo, con Bedrock **mockato** (nessun
costo). Serve solo il tuo account AWS.

1. `aws login` — credenziali AWS attive. (Riferimento account: steering `#aws-accounts`.)
2. Dalla cartella del progetto:
   - `cdk bootstrap` (una tantum per account/region — region: **us-east-1**)
   - `cdk deploy` (default: componenti a pagamento OFF)
3. Recupera l'URL dell'API Gateway `/simulate` dagli output del deploy e invia un
   messaggio di test (POST). Idelia risponde col motore mockato → **costo zero**.
4. A fine test: `cdk destroy`.

Conferma end-to-end della logica su AWS reale, gratis.

---

## FASE B — Bedrock reale (dialogo vero, pochi centesimi) 💰

Per far ragionare Idelia davvero invece del mock.

5. Console AWS → **Bedrock** (region us-east-1) → **Model access** → abilita il
   modello scelto (Claude / Nova). Attivazione una-tantum, gratuita.
6. Imposta l'env var `BEDROCK_MODEL_ID` con l'id del modello.
7. Deploy con flag ON: `cdk deploy -c paid_components_enabled=true`.
   ⚠️ Da qui Bedrock costa a token (pochi cent per test brevi). Billing alarm attivo.

---

## FASE C — Google Calendar (assegnazione slot + scrittura evento) — gratis

8. Google Cloud Console → nuovo progetto → abilita **Google Calendar API**.
9. Crea un **Service Account** → genera chiave **JSON** → scaricala.
10. Condividi i calendari di test (Chiara/Francesca) con l'email del Service
    Account, permesso "modifica eventi".
11. Metti la chiave JSON in **AWS Secrets Manager** (il codice la legge a runtime).

---

## FASE D — Telegram (notifica gruppo + finestra 8h) — gratis

12. Telegram → **@BotFather** → `/newbot` → ottieni il **token** del bot.
13. Crea un **gruppo di test**, aggiungi il bot, recupera il **chat_id** del gruppo.
14. Metti il token in Secrets Manager con il nome atteso dall'env
    `TELEGRAM_BOT_TOKEN_SECRET_NAME`; imposta `TELEGRAM_CHAT_ID` con il chat_id.
15. Dopo il deploy, registra il **webhook** Telegram verso l'endpoint API Gateway
    `/telegram/webhook`.

---

## FASE E — SES (email di consenso) — gratis in sandbox

16. Console **SES** → verifica gli indirizzi email di test (mittente e
    destinatario: obbligatorio finché SES è in sandbox).
17. Carica il **PDF del modulo di consenso** nel bucket S3 (tuo file).

---

## FASE F — Voce: Connect + Lex (ULTIMA, a pagamento) 💰💰

18. Deploy con `paid_components_enabled=true` (già fatto in Fase B) provisiona
    anche Lex V2 e Connect.
19. Console **Connect** → rivendica il **numero di telefono** (💰 canone
    giornaliero) → collega il contact flow al bot Lex.
20. Chiamata di test (pochi minuti). Poi **rilascia il numero** / `cdk destroy`
    per fermare i costi.

> Trappola costi: il canone del numero Connect si paga anche se non chiami nessuno.
> Rilascia il numero o fai `cdk destroy` subito dopo i test.

---

## Riferimenti env var / secret (dal codice)

| Cosa | Dove | Nome atteso |
|------|------|-------------|
| Flag componenti a pagamento | context CDK / env | `paid_components_enabled` / `PAID_COMPONENTS_ENABLED` |
| Modello Bedrock | env Lambda motore | `BEDROCK_MODEL_ID` |
| Nome tabella DynamoDB | env Lambda | `CENTRALINO_TABLE` |
| Nome secret token Telegram | env Lambda notificatore | `TELEGRAM_BOT_TOKEN_SECRET_NAME` |
| Chat id gruppo Telegram | env Lambda notificatore | `TELEGRAM_CHAT_ID` |

I valori esatti (ARN dei secret, chat_id) si passano allo stack come context CDK o
env delle Lambda: verificarli sugli output del deploy quando si arriva alle fasi C/D.

## Comandi utili

```bash
cdk synth                                   # verifica template, costo-zero
cdk deploy                                  # deploy, componenti a pagamento OFF
cdk deploy -c paid_components_enabled=true  # deploy CON Bedrock/Connect/Lex
cdk deploy -c environment=dev               # tag Environment (dev|test|prod)
cdk destroy                                 # rimuove tutto, nessuna risorsa orfana
```
