# Compliance card — Centralino AI Idelia

> Check di conformità EU AI Act + GDPR per questo progetto.
> Riferimento normativo generale: `aws-learning/knowledge-base/bestpract-ai-act-compliance.md`.
> **Natura del progetto:** portfolio SA, **dati finti**, nessuna messa in
> servizio reale. La compliance qui è *by design e dimostrativa*.
> **Ultimo check:** 2026-09-21
> ⚠️ Ausilio tecnico/di studio, NON consulenza legale.

---

## 1. Cosa fa il sistema (scope ai fini della classificazione)

- Riceve chiamate inbound (Amazon Connect + Lex), dialoga in italiano (Bedrock).
- Raccoglie dati anagrafici: nome, età, telefono, **problema** (motivo della
  chiamata), sede, data.
- Sceglie la dottoressa e fissa un colloquio (Google Calendar).
- Notifica il gruppo Telegram delle dottoresse (recap SENZA il campo "problema").
- Invia una mail di consenso (SES).

**Cosa NON fa (confini di design, importanti):**
- ❌ Nessuna diagnosi o consiglio clinico.
- ❌ Nessuna analisi emotiva / del tono di voce (no emotion recognition).
- ❌ Nessuna categorizzazione biometrica.
- Il "problema" è un **dato per l'instradamento**, non una valutazione medica.

## 2. Classificazione EU AI Act

| Livello | Applicabile? | Note |
|---------|--------------|------|
| Art. 5 — pratiche proibite | **No** | Nessuna emotion recognition, biometria, manipolazione o sfruttamento di vulnerabilità |
| Annex III — alto rischio | **No** (di norma) | Non è dispositivo medico, non fa diagnosi né decide cure. È instradamento + prenotazione |
| Art. 50 — trasparenza | **SÌ** | Il paziente interagisce con un'AI conversazionale → deve essere informato |
| Rischio minimo | — | Il resto delle funzioni ricade qui |

**Conclusione:** Idelia è un sistema a **rischio limitato**. L'unico obbligo
sostanziale dell'AI Act che lo riguarda è la **trasparenza (Art. 50)**.

## 3. Checklist di conformità

### AI Act — Trasparenza (Art. 50)
- [ ] **All'inizio della chiamata Idelia dichiara di essere un assistente AI**
      (non la segretaria umana). → *da verificare/aggiungere nel messaggio di
      apertura del contact flow / primo turno Lex.*
- [x] Il campo "problema" (dato sensibile) NON viene inviato al gruppo Telegram
      (già implementato: regola di privacy nel recap).

### GDPR (se un giorno andasse in produzione con dati veri)
- [ ] Informativa privacy al paziente (cosa si raccoglie, perché, per quanto).
- [ ] Base giuridica per i dati di salute: **consenso esplicito** (Art. 9).
      → il flusso mail-consenso (SES) è la base su cui costruirlo.
- [ ] Minimizzazione: raccogliere solo i dati necessari all'appuntamento.
- [ ] Diritti dell'interessato (accesso, rettifica, cancellazione).
- [x] Dati sensibili non diffusi oltre il necessario (problema escluso dal recap).
- [x] Sicurezza: secret in Secrets Manager, IAM per servizio, no segreti nel repo.

### Portfolio (dimostrare la maturità del design)
- [x] Dati finti: nessun dato reale di paziente in gioco.
- [ ] Documentare la scelta "no consigli clinici" come **decisione
      architetturale** (confine di rischio consapevole) nel README/decision log.
- [ ] Menzionare la classificazione AI Act nella slide/README portfolio: fa
      vedere che progetto AI "compliant by design".

## 4. Azioni aperte

1. **Aggiungere il disclaimer AI** nel messaggio di apertura (Art. 50). È
   l'unica vera lacuna sostanziale oggi. Esempio: "Salve, sono Idelia,
   l'assistente virtuale dello studio. La aiuto a fissare un appuntamento."
2. Annotare nel decision log la scelta "solo instradamento, no consigli clinici".
3. Riportare la classificazione nel materiale portfolio.

## 5. Log dei check

| Data | Cosa è stato verificato | Esito |
|------|-------------------------|-------|
| 2026-09-21 | Prima classificazione completa (AI Act + GDPR) | Rischio limitato; unica azione sostanziale = disclaimer AI Art. 50 |
| 2026-09-21 | Sistema verificato end-to-end + teardown risorse | Flusso completo OK (dialogo → prenotazione → Telegram → Calendar → SES). Risorse a pagamento smontate (numero rilasciato, `cdk destroy`, secret cancellati). Disclaimer AI ancora da aggiungere. |
