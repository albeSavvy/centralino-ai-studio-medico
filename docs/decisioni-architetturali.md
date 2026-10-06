# Idelia — Decisioni architetturali (perché questo servizio e non un altro)

> Documento di dettaglio per la review col mentor. Accompagna le slide
> (`docs/presentazione.html`) e il diagramma (`README.md` / `docs/architettura.html`).
> Qui il focus è il **ragionamento ad albero**: problema → vincoli → alternative
> → scelta → trade-off accettato.

## Mappa dei servizi (cosa fa cosa)

| Funzione | Servizio scelto | Alternativa scartata | Perché |
|----------|-----------------|----------------------|--------|
| Telefonia / voce | Amazon Connect + Lex V2 | Twilio, SIP self-managed | Managed, pay-per-use, integrazione nativa con Lambda; niente server voce da gestire |
| Cervello conversazionale | Lambda + Amazon Bedrock | "Lex intelligente" (intent/slot) | Libertà semantica, testabilità senza voce, controllo su timeout/fallback |
| Stato conversazione e dati | DynamoDB single-table | RDS / Aurora | Serverless, zero idle, latenza bassa, accesso per chiave; niente relazioni complesse |
| Orchestrazione conferma 8h | Step Functions | Lambda + self-polling / SQS delay | Attesa lunga nativa, stati espliciti, race wait↔callback gestita dal servizio |
| Notifica + conferma umana | Lambda + Telegram Bot API | SNS / email con link | Bottoni inline, gruppo già usato dalle dottoresse, callback immediato |
| Scrittura appuntamento | Google Calendar (via provider astratto) | Scrittura diretta nel codice | Sink sostituibile (gestionale sanitario poi) senza toccare il motore |
| Email di consenso | Amazon SES | SMTP esterno / SNS | Managed, economico, allegato PDF, integrazione IAM |
| Segreti | AWS Secrets Manager | Variabili d'ambiente / file | Mai credenziali nel codice, rotazione, lettura a runtime con IAM minimo |
| Osservabilità e costi | CloudWatch + AWS Budgets | — | Log/metriche nativi + billing alarm a 1 USD |

## Le 6 decisioni chiave (con trade-off)

### 1. Dove vive il "cervello": Lambda→Bedrock, non "Lex intelligente"
- **Problema:** capire linguaggio naturale libero in italiano e condurre un
  dialogo multi-turno, non solo riconoscere intent predefiniti.
- **Alternativa scartata:** mettere la logica dentro Lex (intent + slot).
- **Scelta:** Lex resta *thin* (solo voce/ASR/TTS), la logica vive in una Lambda
  che chiama Bedrock in tool-use.
- **Trade-off accettato:** una Lambda in più da mantenere e un hop di latenza, in
  cambio di libertà semantica totale, testabilità senza voce (la stessa Lambda è
  invocata dal simulatore testo) e controllo pieno su timeout e fallback.

### 2. Modello dati: DynamoDB single-table, non RDS
- **Problema:** salvare stato conversazione, anagrafiche, appuntamenti con accessi
  rapidi per chiave e per pochi pattern noti.
- **Alternativa scartata:** RDS/Aurora relazionale.
- **Scelta:** DynamoDB single-table (PK/SK, entità PATIENT/DOCTOR/APPT/CONTACT/SESSION,
  GSI1 per nome, GSI2 per stato), billing on-demand.
- **Trade-off accettato:** niente join/query ad-hoc relazionali e design degli
  accessi più rigido in cambio di zero idle cost, latenza bassa e scalabilità
  automatica. Per i volumi di uno studio è la scelta giusta.

### 3. Auth Google Calendar: Service Account + provider astratto
- **Problema:** scrivere sul calendario senza esporre credenziali e potendo
  cambiare in futuro il sink (gestionale sanitario tipo BeebeeDoc).
- **Scelta:** Service Account con chiave JSON in Secrets Manager; interfaccia
  `CalendarProvider` + `GoogleCalendarProvider` con idempotenza e retry.
- **Trade-off accettato:** un livello di astrazione in più, ripagato da
  sostituibilità del sink senza toccare il motore e da credenziali fuori dal codice.

### 4. Finestra conferma 8h: Step Functions, non polling fatto a mano
- **Problema:** aspettare fino a 8h la conferma di una dottoressa, e intanto
  gestire sia la risposta sia lo scadere del tempo (chi arriva prima vince).
- **Alternativa scartata:** Lambda che si auto-richiama / SQS con delay.
- **Scelta:** Step Functions con race tra `Wait (8h)` e callback
  `waitForTaskToken` da Telegram; mutua esclusione garantita da conditional write
  su DynamoDB.
- **Trade-off accettato:** un servizio in più da conoscere, in cambio di attese
  lunghe native, stati visibili/debuggabili e nessun "cron casalingo" fragile.

### 5. Latenza LLM al telefono
- **Problema:** un LLM può essere lento, ma al telefono il silenzio pesa.
- **Scelta:** risposte brevi di default, prompt vincolato, timeout tool-use 10s
  con fallback a stato preservato (nessun dato parziale persistito).
- **Trade-off accettato:** risposte meno "ricche" in cambio di reattività e
  robustezza (il dialogo non si rompe se il modello esita).

### 6. Consenso minori
- **Problema:** un minore non può dare consenso da solo.
- **Scelta:** flag `isMinor` da età (<18) + `parentName` referente obbligatorio;
  email di consenso al referente, mai al minore.
- **Trade-off accettato:** un ramo di logica in più, necessario per correttezza
  legale e di dominio.

## Narrativa per il colloquio: operational → business logic

Idelia nasce come automazione **operativa** (rispondere al telefono e non perdere
richieste). È stata poi evoluta in **logica di business con stati**: conferma
umana, gestione del blocker, finestra temporale di 8h, consenso. È il salto che
dimostra pensiero da Solutions Architect — non "quale servizio aggiungo" ma "quale
problema risolvo e quale trade-off accetto".

## Conformità (EU AI Act)

Classificato **rischio limitato**: Idelia instrada e prenota, non dà consigli
clinici né analizza emozioni. Unico obbligo applicabile: trasparenza (Art. 50) —
dichiarare che è un assistente AI. Dettaglio in `docs/ai-act-compliance.md`.
