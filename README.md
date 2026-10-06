# ArmoredCreator Audio Lab — Evolução Vision-before-Download

> **Laboratório de implementação:** esta branch evolui o comportamento congelado de referência sem alterar o repositório original.
>
> Referência congelada: `armoredcreator/armoredcreator-test`.
> Implementação desta evolução: `armoredcreator/armoredcreator-audio-lab`.
>
> Branch: `feat/source2-vision-before-download`.

---

# 1. Visão geral

O ArmoredCreator recebe conteúdos da fonte Telegram, encontra o produto Shopee exato, gera o contexto para a IA, processa o vídeo, publica o resultado e só finaliza o item depois da confirmação externa e do cleanup.

O princípio central é um único item ativo por vez.

~~~
Telegram fonte
  |
  v
ArmoredSync
  |
  v
RESERVE no SQLite (sem download)
  |
  v
Coordinator
  |
  +----> ArmoredVision V1
  |          |
  |          +----> sem produto exato -> WAITING_VISION
  |          |
  |          +----> affiliate_url persistida
  |
  v
materialização/download do único candidato aceito
  |
  v
ArmoredIA
  |
  v
ArmoredStudio / RVC
  |
  v
ArmoredHub
  |
  v
Telegram destino
  |
  v
CONFIRMED
  |
  v
PUBLISHED
  |
  v
cleanup
~~~

---

# 2. Invariantes

## 2.1 Uma única raiz de composição

O Coordinator é a única raiz que monta Sync, Vision, ArmoredIA, Studio, Hub e Recovery.

## 2.2 Um único item ativo

O item é primeiro reservado no SQLite. A mídia **não é baixada** antes da Vision V1.

~~~
descobrir A
-> reservar A no SQLite
-> Vision V1
   -> WAITING_VISION: sem download
   -> resolvido: persistir affiliate_url
-> materializar A
-> ArmoredIA
-> Studio
-> Hub
-> confirmação
-> cleanup
-> descobrir B
~~~

Não existe pré-download de lote e não existe download para um candidato que a Vision V1 não tenha aceitado.

## 2.3 Nenhuma fila física

Não fazem parte da arquitetura RabbitMQ, Redis, Celery, Kafka, publish_queue ou pastas intermediárias usadas como fila.

## 2.4 SQLite é a fonte de verdade interna

Banco canônico: storage/database/armoredcreator.db

Ele guarda estado, eventos, tentativas, Recovery, checkpoints, contexto V1, contexto ArmoredIA e publicações.

## 2.5 Workspace canônico

Cada item usa storage/videos/{content_id}/.

O ORIGINAL é o artefato imutável de recuperação. Working e result são derivados.

## 2.6 Cleanup

Cleanup só ocorre depois de PUBLISHED e só então o item pode ser considerado concluído fisicamente.

---

# 3. Máquina de estados

~~~
RECEIVED
VISION
IA
STUDIO
PUBLISHING
PUBLISHED
WAITING_VISION
RECOVERY
FAILED
~~~

Fluxo normal com IA habilitada:

~~~
RECEIVED -> VISION -> IA -> STUDIO -> PUBLISHING -> PUBLISHED
~~~

## 3.1 WAITING_VISION

WAITING_VISION significa exclusivamente que a Vision V1 não conseguiu resolver o produto Shopee exato e, portanto, o item não possui destino de publicação naquele momento.

É um estado persistente de conteúdo sem destino por enquanto:

~~~
Vision não encontrou produto Shopee
-> WAITING_VISION
-> permanece no SQLite
-> não publica
-> não é Recovery
-> não bloqueia o CATCH-UP
-> não impede a entrada em LIVE
~~~

Falha de Gemini, Policy, ArmoredIA, Studio ou Hub não deve ser classificada como WAITING_VISION.

## 3.2 RECOVERY

RECOVERY é o estado técnico persistente para um item que precisa ser retomado.

~~~
evidência suficiente -> retomar no ponto seguro
evidência insuficiente -> reconstruir derivados a partir do ORIGINAL
~~~

---

# 4. ArmoredSync

Arquivo principal: ArmoredSync/service.py

Responsabilidades:

- conectar à fonte Telegram;
- descobrir histórico;
- descobrir LIVE;
- localizar exatamente um candidato;
- **reservar o candidato no SQLite sem baixar a mídia**;
- materializar somente depois da aprovação da Vision V1;
- controlar checkpoints;
- administrar o lifecycle da sessão Telethon.

A materialização permanece canônica em `storage/videos/{content_id}/`.

## 4.1 Regra de candidato

Um candidato válido pode ocorrer de três formas:

- vídeo e link Shopee na mesma mensagem;
- vídeo seguido imediatamente pela mensagem não-vídeo que contém o link Shopee;
- álbum Telegram (grouped_id) contendo vídeo(s), foto(s) e um único link Shopee distribuído entre as mídias, em qualquer ordem.

Dentro de um álbum com um único link Shopee, o grupo representa um único conteúdo e um vídeo é escolhido deterministicamente para materialização. Se houver links diferentes no mesmo álbum, somente vídeos que carregam explicitamente seu próprio link são associados; associações ambíguas não são adivinhadas.

O mesmo link Shopee já representado no SQLite não gera uma nova coleta. A deduplicação por URL ocorre na descoberta do Sync; o SyncService de ingestão continua mantendo IDs Telegram distintos como registros independentes quando chamado diretamente.

Não existe busca arbitrária por links em mensagens não relacionadas.

## 4.1.1 Caso histórico 450/451/452 — fechado e comprovado

A auditoria histórica confirmou um único álbum Telegram:

~~~
tópico              = 287
grouped_id          = 14295227350202649
450                  = PHOTO + Shopee 5ardb8fozx
451                  = PHOTO sem link
452                  = VIDEO sem link
~~~

A regra corrigida em `ArmoredSync/service.py` percorre o álbum inteiro quando existe exatamente um link Shopee no grupo e seleciona deterministicamente um vídeo do grupo. Para este caso, o vídeo selecionado foi o `452`, associado ao link `https://s.shopee.com.br/5ardb8fozx`.

A recuperação histórica foi executada e auditada sem alterar o histórico da fonte. O resultado comprovado foi:

~~~
candidato canônico = 452
URL canônica       = 5ardb8fozx
SQLite             = SIM
state              = PUBLISHED
cleanup_completed  = 1
published_message  = 1299
verification       = CONFIRMED
Hub correspondente = SIM
~~~

O caso 450/451/452 está **FECHADO** e constitui evidência real da regra `grouped_id`.

## 4.2 CATCH-UP

O CATCH-UP histórico é progressivo e sequencial e foi concluído integralmente nesta certificação.

~~~
descobrir candidato
-> materializar
-> processar
-> confirmar
-> cleanup
-> próximo candidato
~~~

O checkpoint nunca pode saltar um predecessor que ainda não tenha conclusão segura.

`WAITING_VISION` é uma conclusão segura de classificação para fins de CATCH-UP: o item continua persistido, mas seu checkpoint pode avançar quando não existe um bloqueio técnico anterior. `RECOVERY`, ao contrário, mantém o checkpoint bloqueado até resolução.

## 4.3 LIVE

LIVE usa checkpoints persistidos e preserva o contrato de um único item ativo.

Timeout de descoberta LIVE não é timeout do Studio ou do RVC.

---

# 5. Lifecycle Telegram do Sync

A sessão canônica do Sync fica em credentials/telegram/session/armoredsync.

Reconexão segura:

~~~
desconectar
-> fechar sessão
-> reconstruir client
-> conectar
~~~

Esse ciclo existe para evitar disputa pela sessão SQLite e o antigo database is locked no Windows.

## 5.1 Indisponibilidade temporária da rede — fechado por contrato e testes

A execução real de 30/09 registrou uma queda durante o download do item 697:

~~~
internet cai durante download
-> materialização interrompida por ausência de progresso
-> checkpoint não avança
-> candidato não é concluído
~~~

Esse comportamento permanece protegido.

O Coordinator agora trata falhas transitórias da origem Telegram durante CATCH-UP e LIVE como condições recuperáveis:

~~~
falha transitória
-> liberar sessão
-> reconstruir conexão/iterator
-> retry com backoff
-> manter checkpoint seguro
-> continuar o processo
~~~

O comportamento de reconexão está coberto por testes automatizados. A execução real de 30/09 também demonstrou preservação de checkpoint e recuperação após a interrupção; a cobertura automatizada permanece como proteção adicional.


---

# 6. ArmoredVision V1

Arquivos principais:

~~~
ArmoredVision/service.py
ArmoredVision/modules/v1/shopee_api.py
ArmoredVision/modules/v1/shopee_resolver.py
~~~

A Vision V1 é responsável somente pela identificação Shopee e pela produção do contexto que a etapa de IA precisa.

## 6.1 Fluxo V1

~~~
URL original
-> resolver
-> shop_id + item_id
-> produto exato
-> affiliate link canônico
-> ia_context
-> SQLite
~~~

Vision não gera mais a legenda.
Vision não executa Gemini.
Vision não escolhe candidatas de legenda.

## 6.3 Vision-before-download

Esta branch introduz uma fronteira nova sem duplicar a Vision V1.

~~~
Sync descobre
-> SQLite RECEIVED / reserva
-> Vision V1 usando URL/evidência
-> se não resolver: WAITING_VISION, zero download
-> se resolver: affiliate_url persistida
-> materialização do candidato
-> pipeline normal a partir da evidência persistida
~~~

### Regras

- Vision V1 continua sendo a implementação existente de resolução Shopee.
- `affiliate_url` persistida é a evidência de aceitação para materialização.
- `WAITING_VISION` não materializa mídia.
- Falha técnica da Vision vira `RECOVERY` sem download.
- Um `RECOVERY` sem ORIGINAL é redescoberto na fonte antes do Recovery genérico, porque o Recovery estrito continua exigindo ORIGINAL.
- Falha de download depois da aprovação da Vision também mantém o checkpoint bloqueado e recebe uma tentativa de redescoberta pela fonte.
- Depois que a mídia é materializada, `Pipeline.run()` retoma usando a evidência V1 persistida e não precisa executar Vision novamente.

## 6.2 Contexto durável

~~~
productName
itemId
shopId
shopName
productCatIds
priceMin
priceMax
sales
ratingStar
brand
brandName
model
modelName
description
attributes
technicalCharacteristics
imageUrl
~~~

---

# 7. ArmoredIA

Estrutura:

~~~
ArmoredIA/
├── service.py
├── providers/
│   ├── base.py
│   └── gemini.py
└── caption/
    ├── generator.py
    ├── policy.py
    └── selector.py
~~~

ArmoredIA é a fronteira aberta para futuras IAs e futuras tarefas de IA.

Ela recebe contexto durável da Vision e não precisa redescobrir o produto.

---

# 8. ArmoredIA Caption

## 8.1 Batch de candidatas

O provider Gemini recebe uma única solicitação e pode devolver até 10 candidatas.

Contrato atual:

~~~
1 chamada Gemini
-> até 10 candidatas
-> Policy local
-> todas as candidatas são avaliadas
-> ranking/score local determinístico
-> melhor candidata
~~~

A ordem de chegada não decide mais sozinha a legenda. O score local usa sinais textuais determinísticos do contexto já persistido pela Vision, com o índice original apenas como desempate explícito. Não existe segunda chamada ao Gemini por exaustão da Policy.


Se todas forem rejeitadas:

~~~
1 chamada
-> 0 válidas
-> erro de geração
-> RECOVERY
~~~

Exaustão de Policy não cria segunda chamada Gemini.

## 8.2 Retry

Retry é destinado a problemas técnicos do provider, como timeout, conexão, 429 e erros 5xx.

## 8.3 Sem fallback determinístico

Quando não existe uma legenda válida, a IA falha de forma explícita e o item fica recuperável.

## 8.4 Auditoria persistente das candidatas

Cada batch de caption grava no SQLite todas as candidatas devolvidas, válidas ou rejeitadas, até o limite de 10:

~~~
content_id
batch_id
candidate_index
caption
policy_valid
rejection_reason
score
selected
created_at
~~~

A persistência ocorre tanto quando uma candidata é selecionada quanto quando todas são rejeitadas e o item entra em RECOVERY. Assim, a decisão pode ser reconstruída sem depender dos logs do provider.


---

# 9. Caption Policy

Arquivo: ArmoredIA/caption/policy.py

A Policy é determinística e roda localmente após a resposta do provider.

## 9.1 Formato

~~~
2 ou 3 palavras no texto principal
exatamente 1 emoji
1 ou 2 hashtags
até 20 caracteres por hashtag
~~~

## 9.2 Bloqueios comerciais

Bloqueia linguagem de venda, promoção e urgência, incluindo compre, comprar, garanta, garantir, imperdível, aproveite, oferta, promoção, desconto, corra e não perca.

## 9.3 Embalagem

Bloqueia embalagem, tampa, frasco e lacre.

## 9.4 Especificações

Bloqueia medidas e especificações como ml, cm, g, kg, V, volts, W, watts e outras formas equivalentes quando usadas para expor a ficha técnica.

## 9.5 Marca e modelo

Marca e modelo do contexto V1 não devem aparecer na legenda. Modelos curtos também são protegidos.

## 9.6 Nome do produto

A Policy bloqueia combinações contíguas distintivas do título, sem proibir toda palavra que apareça no nome.

Exemplo:

~~~
Produto: Batom Matte Vermelho

Permitido:
Olha isso ✨
#beleza

Bloqueado:
Batom matte ✨
#beleza
~~~

## 9.7 Hashtag

Uma hashtag que reconstrua explicitamente uma combinação distintiva do título é rejeitada.

## 9.8 Contexto natural

Descrição, categoria, ambiente, uso e características comuns podem compartilhar palavras com a legenda.

Isso reduz falsos negativos e impede que a Policy vire um filtro excessivamente restritivo.

---

# 10. ArmoredStudio

Arquivos principais:

~~~
ArmoredStudio/service.py
ArmoredStudio/unified.py
ArmoredStudio/analysis/*
ArmoredStudio/processing/*
~~~

Fluxo:

~~~
ORIGINAL
-> análise
-> plano
-> RVC
-> FFmpeg
-> RESULT
~~~

## 10.1 RVC

Runtime padrão: ArmoredStudio/runtime/rvc/

Voz utilizada no ambiente real desta fase: melody.

RVC não possui downgrade silencioso.

## 10.2 Story

A saída final é 1080 x 1920 e mantém proporção, usando crop quando necessário.

## 10.3 Console

O console não deve mostrar dump do modelo ou parâmetros internos do backend.

Detalhes técnicos devem continuar disponíveis nos logs.

---

# 11. ArmoredHub

Arquivo principal: ArmoredHub/service.py

Responsabilidades:

- publicação;
- idempotência;
- reconciliação;
- confirmação;
- proteção contra republicação.

## 11.1 Transporte

O ambiente real usa Telegram Bot API local em 127.0.0.1:8081.

A reconciliação permanece baseada em Telethon.

## 11.2 publish_once

Para item novo:

~~~
publication inexistente
-> publish
-> message_id
-> confirmação
~~~

Não se deve pré-criar uma publication que faça item novo entrar no caminho de reconciliação como se já tivesse sido publicado. A chamada publish_once() pode registrar internamente o início da publicação depois de constatar que não existe registro anterior; isso é diferente de criar a publication antes da decisão de novo envio.

## 11.3 Hub Preflight — melhoria futura fora do fechamento atual

O Hub Preflight permanece registrado no Issue #44 como melhoria de eficiência e hardening.

Ele não faz parte dos gates desta versão, não substitui publish/reconciliação e não é necessário para concluir a certificação atual. O fluxo de publicação real e sua reconciliação continuam sendo a autoridade externa.


## 11.4 CONFIRMED

CONFIRMED exige mensagem real e message_id válido.

## 11.5 UNKNOWN

UNKNOWN significa evidência insuficiente.

~~~
UNKNOWN
-> RECOVERY
-> não republicar automaticamente
~~~

## 11.6 ABSENT

ABSENT exige evidência suficiente de ausência.

Uma operação externa potencialmente executada não pode ser tratada como inexistente apenas porque uma busca inicial não encontrou evidência.

## 11.7 Reconciliação

~~~
busca contextual
-> candidato
-> verificação por message_id
-> confirmação exata
~~~

Metadado de tópico ausente pode ser tolerado quando a consulta não fornece esse campo.
Metadado explicitamente incompatível deve ser rejeitado.
Múltiplos matches não devem ser escolhidos arbitrariamente.

---

# 12. Recovery

Arquivo: armored_core/recovery.py

## 12.1 Falha da ArmoredIA

Quando V1 já resolveu e persistiu contexto:

~~~
VISION
-> IA
-> falha
-> RECOVERY
-> IA
-> STUDIO
~~~

A Vision não deve ser repetida somente por causa da falha da IA.

## 12.2 Falha do Studio

Derivados podem ser reconstruídos a partir do ORIGINAL.

## 12.3 Publicação pendente

Publication existente deve ser reconciliada antes de produzir um novo efeito externo.

## 12.4 Resultado durável

Resultado final comprovado pode ser retomado diretamente para publicação.

---

# 13. Startup Audit

Arquivo: armored_core/startup_audit.py

O startup verifica itens, estados, publicações, cleanup, workspaces, órfãos e evidências históricas.

Falhas legadas de Caption que foram classificadas incorretamente como WAITING_VISION podem ser migradas para RECOVERY quando a evidência for inequívoca.

Essa migração preserva o significado de WAITING_VISION como estado exclusivo da Vision V1.

---

# 14. Credenciais e configuração

Segredos ficam em credentials/project.env.
Configuração operacional fica em .env.

Valores secretos não são registrados no Git.

Configuração ArmoredIA usada no launcher:

~~~
ARMORED_IA_ENABLED=1
ARMORED_IA_CAPTION_ENABLED=1
ARMORED_IA_MODEL=gemini-3.1-flash-lite
ARMORED_IA_API_TIMEOUT=90
ARMORED_IA_MAX_CANDIDATES=10
ARMORED_IA_MAX_ATTEMPTS=5
ARMORED_IA_RETRY_DELAY=2
~~~

Destino do laboratório:

~~~
ARMORED_CREATOR_GROUP_ID=-1004341972306
ARMORED_HUB_TOPIC_ID=228
~~~

Fonte histórica do laboratório:

~~~
ARMORED_SYNC_SOURCE=-1003788989075
ARMORED_SYNC_SOURCE_ID=-1003788989075
~~~

O código atual descobre os tópicos do fórum e não usa ARMORED_SYNC_TOPIC_NAME como filtro exclusivo.

---

# 15. START_ALL

START_ALL.bat é o launcher operacional único.

Ele configura ArmoredIA, localiza e inicia o Bot API local, localiza Python e chama run_coordinator.py.

Ele não implementa uma pipeline paralela.

---

# 16. Observabilidade

## 16.1 Console

O console é a superfície do operador.

Deve mostrar somente item, etapa, progresso essencial, conclusão e erros reais.

## 16.2 Trace

Arquivo: storage/logs/pipeline_trace.jsonl

O trace mantém eventos estruturados de início, fim, duração, transições, Recovery, erros e confirmação.

## 16.3 Logs internos

RVC e análises podem manter detalhes completos em arquivo sem despejar esses dados no terminal.

---

# 17. Testes automatizados

A suíte do laboratório cobre a arquitetura existente e os gates da evolução Vision-before-download.

Casos críticos desta branch:

~~~
Vision aceita
-> materializer chamado
-> ORIGINAL criado
-> pipeline normal pode continuar

Vision não resolve
-> WAITING_VISION
-> materializer nunca chamado

Vision falha tecnicamente
-> RECOVERY
-> materializer nunca chamado
-> fonte pode redescobrir o candidato

download falha depois da Vision
-> checkpoint permanece bloqueado
-> uma redescoberta pode tentar novamente
~~~

O GitHub Actions do commit `0eab927c5741951ef6a0184d6aa0164052665fda` concluiu o job `unit` com sucesso no run `#15`.

Comando principal:

~~~
python -m pytest -q -W error::RuntimeWarning
~~~

Verificação adicional:

~~~
git diff --check
~~~

# 18. Evidência operacional real em 29/09–01/10/2026

## Item 412

~~~
RECOVERY
-> VISION
-> IA
-> STUDIO/RVC
-> HUB
-> Telegram CONFIRMED #1005
-> PUBLISHED
-> cleanup
~~~

## Item 392

~~~
RECOVERY
-> VISION
-> IA
-> STUDIO/RVC
-> HUB
-> Telegram CONFIRMED #1007
-> PUBLISHED
-> cleanup
~~~

Esses casos comprovam integração real de Recovery, Vision, ArmoredIA, Studio, Hub, Telegram e cleanup.

Na certificação histórica anterior, também foram observados, com publicação real e cleanup:

~~~
1383
1174
823
706
564
563
698
550
767
~~~

Em todos esses casos o log observou a sequência necessária até PUBLISHED + cleanup.

O item 823 é uma evidência importante de download lento: 25,5 MiB foram materializados em aproximadamente 239,2s e o pipeline continuou normalmente até publicação e cleanup.

O item 564 produziu avisos do decoder H.264 (mmco: unref short failure) durante o Studio, mas terminou normalmente em Hub, confirmação Telegram e cleanup. O aviso não foi promovido a falha do pipeline.

Esses resultados comprovam operação real, mas não substituem a prova específica de uma falha dentro da ArmoredIA seguida de Recovery direto na própria IA.

## 18.1 Conteúdos LIVE reais observados em 01/10/2026

Na fonte Telegram real `-1003788989075`, o Coordinator estava em modo LIVE e novos conteúdos entraram espontaneamente. Três conteúdos foram capturados e concluídos de ponta a ponta:

~~~
2444 -> download -> Vision -> ArmoredIA -> Studio/RVC -> Hub -> CONFIRMED -> PUBLISHED + cleanup
2447 -> download -> Vision -> ArmoredIA -> Studio/RVC -> Hub -> CONFIRMED -> PUBLISHED + cleanup
2448 -> download -> Vision -> ArmoredIA -> Studio/RVC -> Hub -> CONFIRMED -> PUBLISHED + cleanup
~~~

Registros finais observados no console:

~~~
[PIPELINE][ITEM 2444] FINALIZADO PUBLISHED+cleanup
[PIPELINE][ITEM 2447] FINALIZADO PUBLISHED+cleanup
[PIPELINE][ITEM 2448] FINALIZADO PUBLISHED+cleanup
~~~

Esta é evidência operacional real de conteúdo novo chegando em LIVE, sendo materializado e percorrendo Vision, ArmoredIA, Studio/RVC, Hub, confirmação Telegram e cleanup. A exigência de esperar indefinidamente por conteúdo espontâneo para comprovar o mecanismo LIVE está encerrada.

---

# 19. Auditoria real de startup e estado persistente

Resumo observado anteriormente:

~~~
120 itens inventariados
76 publicados
44 pendentes
0 falhos
76 limpos
120 workspaces
0 órfãos
76 publicações confirmadas
5 ambíguas
~~~

## 19.0 Snapshot operacional de 01/10/2026 antes da rodada LIVE

Às `15:24:40`, antes dos três novos conteúdos LIVE desta rodada, o Startup Audit registrou:

~~~
itens                   = 310
publicados              = 286
pendentes               = 24
falhos                  = 0
limpos                  = 286
workspaces              = 310
órfãos                  = 0
publicações_confirmadas = 286
ambíguas                = 0
~~~

Depois desse snapshot, os itens `2444`, `2447` e `2448` foram processados e finalizaram com `PUBLISHED + cleanup`.

Publicações históricas ambíguas foram verificadas conforme o contrato do Hub.

## 19.1 Snapshot formal observado em 30/09/2026 após reinício

Depois da interrupção causada pela perda real de internet, o processo foi reiniciado. O Startup Audit encontrou:

~~~
itens                   = 209
publicados              = 128
pendentes               = 81
falhos                  = 0
limpos                  = 128
workspaces              = 209
órfãos                  = 0
publicações_confirmadas = 128
ambíguas                = 0
~~~

Esse snapshot comprova que o SQLite preservou o estado de 209 itens, sem órfãos e sem transformar a interrupção de rede em uma massa de itens FAILED.

## 19.2 Snapshot operacional externo usado para o teste

Antes da execução histórica zerada, o tópico do Hub foi limpo exclusivamente por script:

~~~
Grupo Hub          : -1004341972306
Tópico             : 228
Mensagens antes    : 79
Mensagens apagadas : 79
Tópico raiz        : preservado
Grupo fonte        : não tocado
~~~

Esse procedimento criou um destino externo limpo sem apagar o histórico da fonte Telegram.

---

# 20. Teste histórico do zero — execução real

O teste histórico real preservou o histórico Telegram e observou a cadeia sequencial de descoberta, materialização, Vision, ArmoredIA, Studio, Hub, CONFIRMED, PUBLISHED e cleanup.

A execução também registrou uma perda real de conectividade durante o item 697. O checkpoint permaneceu preservado, o item não foi convertido indevidamente em PUBLISHED/FAILED definitivo e o processo pôde ser reiniciado mantendo o estado SQLite.

A implementação atual adiciona tratamento de falhas transitórias no Coordinator e reconstrução do iterator no CATCH-UP, além de teste controlado da sobrevivência do loop LIVE. A evidência real de 30/09 demonstrou interrupção durante materialização sem avanço indevido do checkpoint; o processo foi reiniciado com o estado SQLite preservado.

O estado interno, os checkpoints e o histórico externo não devem ser zerados para realizar essa revalidação.

# 21. CATCH-UP -> LIVE

Chegar ao fim do iterador não basta.

O mecanismo de cutover CATCH-UP -> LIVE está coberto por teste automatizado controlado e, em 01/10/2026, o processo recebeu conteúdo novo real em LIVE: `2444`, `2447` e `2448` chegaram a `PUBLISHED + cleanup`.

A evidência espontânea de LIVE já existe. A certificação histórica da Fonte 1, incluindo a recuperação do grupo `450/451/452`, também foi concluída e auditada.

Condição de entrada:

~~~
histórico esgotado
+ nenhum bloqueio técnico
+ itens elegíveis concluídos ou classificados como WAITING_VISION
+ publicações confirmadas
+ cleanup concluído
+ zero órfãos
-> LIVE
~~~

Recovery pendente não pode ser ignorado para entrar em LIVE.

# 23. Critérios de fechamento histórico

O freeze da versão atual depende de evidência, não apenas da existência de testes unitários.

~~~
[x] Selector compara todas as candidatas válidas
[x] ranking/score local é determinístico e testado
[x] primeira candidata válida não é mais escolhida por ordem de chegada

[x] cada candidata do batch é persistida no SQLite
[x] motivo de cada rejeição é persistido
[x] score é persistido
[x] candidata selecionada é identificável no histórico

[x] Hub Preflight permanece fora do escopo desta versão; Issue #44 é melhoria futura

[x] Coordinator permanece vivo diante de erro transitório em LIVE
[x] reconexão/reset do iterator em CATCH-UP está coberto por teste
[x] checkpoint permanece seguro durante a recuperação
[x] revalidação operacional de perda real de internet

[x] falha da ArmoredIA gera RECOVERY
[x] Recovery da ArmoredIA volta diretamente para IA
[x] Vision não é repetida nesse caso

[x] CATCH-UP histórico completo desde checkpoint zero
[x] nenhum candidato legítimo ficou para trás
[x] nenhum predecessor foi pulado
[x] checkpoints finais conferidos

[x] WAITING_VISION ocorre somente para unresolved real da Vision V1
[x] WAITING_VISION não bloqueia CATCH-UP
[x] WAITING_VISION não impede LIVE
[x] WAITING_VISION permanece persistido no SQLite
[x] nenhum erro técnico de IA/Studio/Hub é mascarado como WAITING_VISION

[x] Studio e RVC completos
[x] Hub idempotente
[x] CONFIRMED com message_id real
[x] UNKNOWN não republica automaticamente
[x] cleanup somente após PUBLISHED
[x] zero órfãos em testes/auditorias já executados

[x] mecanismo controlado de CATCH-UP -> LIVE
[x] evidência operacional de conteúdo LIVE real — 2444, 2447 e 2448

[x] suíte automatizada verde do código final — 175 passed, 1 skipped
[x] CI verde do código final — runs #946 e #947
[x] README atualizado com o snapshot e as pendências reais
[x] CI verde desta atualização documental
[x] versão congelada/tagueada — `v1.0.0-certified`
~~~

# 24. Estado de certificação

## Fechado por implementação e testes

~~~
Coordinator
SQLite
Storage
Recovery
Startup Audit
Sync
Telegram lifecycle
Vision V1
ArmoredIA
Caption Policy
caption ranking
auditoria individual de candidatas
Studio
RVC
Hub
reconciliação
idempotência
cleanup
CATCH-UP -> LIVE controlado
~~~

## Comprovado em execução real nesta fase

~~~
Vision -> ArmoredIA
Gemini real
Studio/RVC real
RVC voice=melody
Hub real
Telegram CONFIRMED
PUBLISHED + cleanup
Recovery real
restart com SQLite persistente
checkpoint preservado após falha de materialização
0 órfãos em Startup Audit observado
~~~

## Evidência operacional concluída nesta fase

~~~
revalidação real de perda temporária de internet
CATCH-UP histórico completo desde checkpoint zero
nenhum candidato legítimo ficou para trás
nenhum predecessor foi pulado
conferência dos checkpoints finais dos tópicos elegíveis
recuperação específica do grupo 450/451/452
registro do 5ardb8fozx no SQLite após a recuperação
~~~

O grupo 450/451/452 foi recuperado como candidato `452`, publicado, confirmado e limpo. O histórico completo da Fonte 1 foi auditado sem apagar o banco ou o histórico Telegram.

## Evidência LIVE já obtida

~~~
01/10/2026
2444 -> PUBLISHED + cleanup
2447 -> PUBLISHED + cleanup
2448 -> PUBLISHED + cleanup
~~~

A evidência operacional de conteúdo espontâneo em LIVE está fechada. A pendência histórica do grupo 450/451/452 e do link 5ardb8fozx também está encerrada.

## Fora do escopo desta versão

~~~
Hub Preflight
espera por conteúdo LIVE espontâneo como condição de freeze
~~~



# 25. Regra operacional final

~~~
SYNC
-> descobre e materializa

VISION V1
-> identifica
-> persiste contexto
-> unresolved = WAITING_VISION

ARMOREDIA
-> até 10 candidatas
-> Policy local
-> todas as válidas
-> ranking local determinístico
-> melhor candidata
-> exaustão = RECOVERY
-> falha técnica = RECOVERY
-> não reroda Vision

STUDIO
-> análise
-> RVC
-> FFmpeg

HUB
-> publish_once
-> reconciliação
-> CONFIRMED / ABSENT / UNKNOWN
-> UNKNOWN não republica automaticamente

CLEANUP
-> somente após PUBLISHED

CATCH-UP
-> um por vez
-> checkpoint seguro
-> Recovery posterior

LIVE
-> somente após histórico seguro
~~~

O objetivo da certificação é provar que cada efeito externo, cada mudança de estado e cada avanço de checkpoint possui evidência persistente e recuperável.

# 26. Escopo bloqueado antes do próximo grupo fonte

A versão atual é a **Fonte 1 certificada e congelada**. O commit certificado é `69fb9a5fc771298dafc04fdc420ff1a0bd8ec227` e a tag oficial é `v1.0.0-certified`.

O grupo fonte atual permanece:

~~~
ARMORED_SYNC_SOURCE=-1003788989075
ARMORED_SYNC_SOURCE_ID=-1003788989075
~~~

Qualquer segundo grupo fonte será **aditivo** e jamais substituirá a Fonte 1.

A Fonte 1 está encerrada para esta versão. Qualquer nova fonte ou mudança funcional deverá ser tratada como novo ciclo de desenvolvimento, sem alterar a versão certificada.

A Fonte 2 deverá ter seus próprios parâmetros e checkpoints sem romper o princípio global de um único item ativo. O comportamento do Sync atual de descobrir os tópicos do fórum também deve continuar explícito: ARMORED_SYNC_TOPIC_NAME não é hoje um filtro exclusivo.

---

# 27. Regra de manutenção

O baseline oficial continua intocado.

A versão certificada atual é a referência funcional única deste repositório. Não há branches de desenvolvimento necessários para executar a versão certificada.

Qualquer mudança futura deve ser isolada em branch própria, passar pela suíte completa, passar por `git diff --check` e obter evidência operacional antes de ser tratada como uma nova versão.