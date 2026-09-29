# ArmoredCreator — Estado Final de Certificação

> Laboratório de reconstrução e certificação: `armoredcreator/armoredcreator-test`.
> O repositório oficial `armoredcreator/armoredcreator` e a branch `audit/baseline-2026-09-19` permanecem intocados.

## 1. Estado atual — 29/09/2026

Esta é a referência operacional da versão atual.

### E2E real mais recente

Dois itens reais foram processados sem inserção manual de vídeo:

```text
1383 → Vision V1 + Caption → Studio/RVC → Hub → Telegram CONFIRMED #922 → PUBLISHED → cleanup
706  → Vision V1 + Caption → Studio/RVC → Hub → Telegram CONFIRMED #923 → PUBLISHED → cleanup
```

O item 706 é especialmente relevante porque havia falhado historicamente na validação da Caption. Na execução atual ele passou pela Vision, Studio, RVC, Hub, confirmação Telegram e cleanup.

### Caption V1

A Caption pertence à Vision V1 e não identifica nem substitui o produto.

```text
ARMORED_CAPTION_ENABLED=1
ARMORED_CAPTION_MODEL=gemini-3.1-flash-lite
ARMORED_CAPTION_API_TIMEOUT=90
ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK=0
```

Regras atuais: português do Brasil; 2–3 palavras no texto principal; exatamente 1 emoji; 1–2 hashtags; reação específica ao produto; sem embalagem/tampa/frasco/lacre; sem marca/modelo; sem linguagem comercial; sem reprodução de combinação distintiva do título. Palavras genéricas do tipo do produto podem ser usadas naturalmente.

### Vision V2

**Fora desta versão.** Não existe módulo V2 nem `validate_vision_v2_real.py` nesta branch. Branches antigas de V2 não fazem parte da versão final e não devem ser reintroduzidas.

### Auditoria de certificação — 29/09

A revisão módulo a módulo identificou e corrigiu somente bloqueios comprovados no contrato atual:

- Gemini/API e ausência de credencial agora são tratados como falha técnica RECOVERY; falhas inesperadas de programação/configuração da Caption não são convertidas em WAITING_VISION.
- CATCH-UP não executa Recovery inline durante o scan histórico; ele termina o scan atual e só depois tenta Recovery.
- Quando Recovery realmente progride, o iterador histórico é reconstruído para permitir a retomada no mesmo processo sem perder o checkpoint durável.
- Falha de materialização deixa a reserva RECEIVED sem original e recebe uma única tentativa de rediscovery no mesmo processo; nova falha encerra sem loop.
- PUBLISHED com cleanup_completed=0 participa da Recovery de startup e bloqueia a progressão até o cleanup terminar.
- Migrações SQLite não removem mais tabelas históricas de evidência da Vision.
- Valores reais de origem/grupo/tópico do ambiente não ficam mais como defaults no código ou em .env.example; a configuração operacional deve vir do ambiente/credentials/project.env.

A suíte completa deve ser executada novamente antes de qualquer CATCH-UP histórico real. O CI da branch foi ajustado para executar pytest sobre toda a suíte e tratar RuntimeWarning como erro.

---

## 2. Arquitetura definitiva

Uma única raiz de composição: **Coordinator**.

```text
Telegram fonte
  ↓
ArmoredSync
  ↓
SQLite + storage/videos/{content_id}/
  ↓
Coordinator
  ├─ Vision V1 + Caption
  ├─ Studio + RVC + FFmpeg
  └─ Hub
       ↓
Telegram destino / topic 228
       ↓
confirmação
       ↓
PUBLISHED
       ↓
cleanup
       ↓
próximo item
```

Invariantes: exatamente 1 item ativo; processamento sequencial; SQLite como verdade interna; workspace único por item; sem RabbitMQ/Redis/Celery/Kafka; sem filas físicas; sem pré-download de lote; checkpoint só avança após conclusão; UNKNOWN nunca gera republicação automática.

---

## 3. ArmoredSync

Arquivo principal: `ArmoredSync/service.py`.

- Telegram fonte real.
- Candidato = vídeo + link na mesma mensagem, ou vídeo + link na mensagem imediatamente seguinte.
- Materialização diretamente no workspace canônico.
- CATCH-UP progressivo, um candidato por vez.
- LIVE em rodízio de tópicos, com checkpoints persistidos.
- Não executa Vision, Studio ou Hub.
- Lifecycle Telethon reconstruível; o teste Wi-Fi OFF/ON comprovou reconexão sem o antigo `database is locked`.

**Estado: FECHADO/CERTIFICADO pelos testes automatizados e reais acumulados.**

---

## 4. Core / Coordinator

Arquivos centrais: `models.py`, `database.py`, `storage.py`, `services.py`, `pipeline.py`, `recovery.py`, `startup_audit.py`, `production_contracts.py`, `trace.py`, `coordinator.py`.

- Estados persistidos e publicação `CONFIRMED/ABSENT/UNKNOWN`.
- SQLite, checkpoints, runtime lock e auditoria.
- Pipeline sequencial RECEIVED → VISION → STUDIO → PUBLISHING → PUBLISHED.
- Falhas de processamento entram em RECOVERY em vez de serem abandonadas.
- Recovery reconcilia SQLite + filesystem + Telegram.
- Startup reabre estados legados FAILED através do caminho de recovery.
- Coordinator é a única composição e controla CATCH-UP → LIVE.

**Estado: FECHADO.**

---

## 5. ArmoredVision V1

Arquivos: `ArmoredVision/service.py`, `modules/v1/shopee_api.py`, `modules/v1/shopee_resolver.py`, `modules/v1/caption/generator.py`, `modules/v1/caption/policy.py`.

- Resolve URL Shopee para `shop_id + item_id`.
- Consulta o produto exato pela API de afiliados.
- O link recebido de outra afiliada é somente identidade; não é o link final.
- `offerLink` é preferido quando disponível; fallback usa o `productLink` canônico.
- Caption usa contexto de produto já identificado pela V1.
- Gemini 3.1 Flash-Lite real foi validado.
- Caption válida é requisito para avançar.

**Estado: FECHADO/CERTIFICADO em integração real.**

---

## 6. ArmoredStudio

Arquivos: `ArmoredStudio/service.py`, `unified.py`, `analysis/*`, `processing/*`.

- Studio único e unificado.
- Análise → plano → processamento → finalização.
- Blackbar/banner/VEO/Gemini/exportação fazem parte da camada de análise/plano.
- RVC faz parte do Studio.
- Runtime RVC e voz `melody` foram usados em execução real.
- CPU fallback funcionou.
- Itens 1383 e 706 produziram áudio RVC e vídeo final.
- Finalizer padroniza a saída para Story 1080×1920: escala proporcional + crop central para fontes não-9:16; sem stretch, padding ou blur no vídeo principal.
- O `crop_final` da análise acontece antes da normalização Story.

**Estado: FECHADO; normalização Story coberta por teste de regressão.**

---

## 7. ArmoredHub

Arquivo: `ArmoredHub/service.py`.

Contrato definitivo de publicação e reconciliação:

- SQLite registra a intenção de publicação antes do efeito externo.
- `CONFIRMED` exige mensagem Telegram válida e nunca republica.
- `UNKNOWN` é inconclusivo e segue para Recovery; nunca autoriza republicação automática.
- `SENT_UNVERIFIED` significa que o envio externo já começou. Zero evidência após todas as tentativas permanece `UNKNOWN`, nunca `ABSENT`.
- Candidato encontrado em busca/histórico global é apenas candidato. Quando existe exatamente um, o Hub reconsulta a mensagem pelo próprio `message_id` antes de confirmar.
- Metadado de tópico ausente é aceito quando a consulta é topic-scoped ou quando a busca global permite explicitamente metadado desconhecido.
- Metadado de tópico explicitamente diferente do tópico esperado é sempre rejeitado, inclusive na verificação por `message_id`.
- Nome de arquivo não é critério de identidade da publicação.
- Múltiplos matches exatos permanecem `UNKNOWN` para evitar seleção arbitrária.

A correção desta branch fecha o risco de republicação após envio ambíguo e separa descoberta de candidato da confirmação por ID.

Execuções reais históricas: 1383 → `CONFIRMED #922`; 706 → `CONFIRMED #923`.

**Estado do código: FECHADO quanto ao contrato de reconciliação. A certificação operacional da branch depende da execução da suíte após esta última alteração.**

---

## 8. Storage e cleanup

Workspace canônico: `storage/videos/{content_id}/`.

SQLite: `storage/database/armoredcreator.db`.

Cleanup só ocorre depois de `PUBLISHED` confirmado. Filas físicas antigas não fazem parte da arquitetura.

**Estado: FECHADO.**

---

## 9. START_ALL

`START_ALL.bat` é o launcher operacional único e chama `run_coordinator.py`, que cria o Coordinator.

Caption V1 e Gemini 3.1 Flash-Lite ficam ativos no launcher real.

**Estado: FECHADO.**

---

## 10. Testes automatizados

A suíte atual cobre arquitetura, invariantes, SQLite, pipeline, Recovery, startup audit, runtime lock, Sync, CATCH-UP, restart, publicação, confirmação Telegram, reconnect, LIVE polling, RVC, Vision/Studio e Caption.

A última execução registrada antes da correção final do Hub foi:

```text
135 testes executados
1 falha: test_unscoped_candidate_is_reverified_by_message_id
134 demais testes passaram
```

A falha foi corrigida no código de teste: o teste anterior substituía `_find_telegram_publications()` e, portanto, eliminava justamente a etapa de produção que deveria ser verificada. O teste agora mantém o método real e intercepta somente a fronteira assíncrona de descoberta, validando `candidato 474 → verificação por message_id → CONFIRMED`.

A implementação também foi corrigida para preservar `UNKNOWN` quando `SENT_UNVERIFIED` termina sem evidência exata e para rejeitar metadado de tópico explicitamente incompatível.

**Estado: código e testes corrigidos; a suíte não foi reexecutada nesta alteração e este README não declara uma nova execução verde sem evidência.**

---

## 11. Certificações reais acumuladas

- Telegram fonte real.
- CATCH-UP progressivo.
- Pipeline sequencial.
- Vision V1.
- Caption Gemini dentro da pipeline real.
- Studio/RVC/FFmpeg.
- Hub e Telegram destino.
- Confirmação real.
- Cleanup.
- Restart.
- Recovery.
- Wi-Fi OFF/ON + reconstrução da sessão Telethon.
- Proteção contra republicação.
- Power-loss recovery no item 1383.
- CATCH-UP limitado → LIVE em testes anteriores.

---

## 12. O que falta para declarar certificação histórica 100%

O código de produção e o contrato de reconciliação Telegram estão fechados nesta branch. A certificação histórica 100% continua sendo uma etapa operacional separada:

```text
CATCH-UP histórico completo
≈ 308 conteúdos elegíveis
→ processamento sequencial
→ confirmação
→ cleanup
→ checkpoints
→ entrada automática em LIVE
```

Essa etapa não exige inserir vídeo manualmente. O Sync deve descobrir os conteúdos existentes na fonte real.

A ausência de um novo conteúdo LIVE artificialmente injetável é uma limitação do laboratório, não uma lacuna da arquitetura.

Antes dessa certificação histórica, a suíte automatizada deve ser executada sobre o código final para registrar evidência objetiva da branch.

---

## 13. Critérios para fechar o histórico

- todos os candidatos elegíveis percorridos;
- nenhum bloqueio sem motivo registrado;
- checkpoints persistidos;
- publicações confirmadas registradas;
- cleanup correto;
- zero órfãos;
- zero filas físicas;
- zero republicações indevidas;
- histórico concluído;
- Coordinator em LIVE.

---

## 14. Congelamento

A branch recebeu o fechamento final do contrato Telegram em 29/09/2026:

- `SENT_UNVERIFIED + zero matches` permanece `UNKNOWN`.
- Candidato global único é revalidado por `message_id`.
- Tópico explicitamente incompatível é rejeitado.
- Metadado de tópico ausente continua aceito somente nos contextos em que a consulta não fornece essa evidência.
- O teste de reconciliação foi corrigido para exercitar o caminho real de produção.

Essas alterações são mínimas e não reabrem Coordinator, Sync, Vision V1, Caption, Studio ou Core.

A branch ainda não deve ser marcada como certificação operacional final sem a evidência da suíte após a alteração.

Base da versão congelada antes da correção Story:
```text
a345f2fd997d841e1e38f8e4609ed4af7083dd30
```

Correção Story integrada na branch congelada pelo PR #29. Os commits atuais da branch devem ser usados no fechamento final.

---

## 15. Checklist

| Módulo | Estado |
|---|---|
| Coordinator | ✅ fechado |
| SQLite / Database | ✅ fechado |
| Storage | ✅ fechado |
| Recovery | ✅ fechado |
| Startup Audit | ✅ fechado |
| ArmoredSync | ✅ fechado |
| Telegram lifecycle | ✅ fechado |
| Vision V1 / Shopee | ✅ fechado |
| Caption Gemini V1 | ✅ fechado |
| Caption Policy | ✅ fechado |
| ArmoredStudio | ✅ fechado |
| RVC | ✅ fechado |
| FFmpeg / Finalizer | ✅ fechado + Story 9:16 |
| ArmoredHub | ✅ contrato de reconciliação fechado |
| Telegram publication | ✅ contrato de idempotência fechado |
| Confirmation | ✅ contrato fechado |
| Cleanup | ✅ fechado |
| START_ALL | ✅ fechado |
| Automated suite | 🟡 corrigida; execução pós-correção pendente |
| Vision V2 | 🚫 fora da versão |
| CATCH-UP histórico ≈308 | ⏳ certificação operacional |
| LIVE novo conteúdo | ⏳ não reproduzível artificialmente |
| Certificação histórica 100% | ⏳ após suíte + CATCH-UP |

---

## 16. Regra operacional final

O estado técnico desta branch agora é:

```text
CÓDIGO DE PRODUÇÃO
└── FECHADO quanto ao contrato Telegram/reconciliação

TESTE DE RECONCILIAÇÃO
└── CORRIGIDO para exercitar o caminho real

SENT_UNVERIFIED
└── ZERO EVIDÊNCIA = UNKNOWN

CANDIDATO GLOBAL ÚNICO
└── REVALIDAÇÃO OBRIGATÓRIA POR message_id

TÓPICO EXPLÍCITO DIFERENTE
└── REJEITADO

V2
└── FORA DA VERSÃO

CATCH-UP ≈308
└── CERTIFICAÇÃO OPERACIONAL PENDENTE
```

A partir deste ponto, não há motivo arquitetural para reabrir Coordinator, Sync, Vision V1, Caption, Studio, Storage ou Core por causa do incidente Telegram.

O próximo marco é exclusivamente de evidência operacional: executar a suíte final, registrar o resultado real e então realizar o CATCH-UP histórico. Se o CATCH-UP concluir com checkpoints, confirmações, cleanup e zero órfãos conforme o contrato, registrar o commit final e congelar a versão.
