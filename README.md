# ArmoredCreator — Arquitetura Final, Contrato Operacional e Certificação

> Laboratório de reconstrução e certificação: armoredcreator/armoredcreator-test.
> O repositório oficial armoredcreator/armoredcreator e a branch audit/baseline-2026-09-19 permanecem intocados.

Este documento é a referência operacional da versão atual. Ele descreve o fluxo completo, os limites de cada componente, as regras de recuperação, a publicação Telegram, a política de legenda, a observabilidade e os critérios para a certificação histórica.

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
SQLite + workspace canônico
  |
  v
Coordinator
  |
  +----> ArmoredVision V1
  |          |
  |          +----> contexto V1 persistido
  |
  +----> ArmoredIA
  |          |
  |          +----> legenda válida
  |
  +----> ArmoredStudio
  |          |
  |          +----> vídeo final
  |
  +----> ArmoredHub
             |
             v
       Telegram destino
             |
             v
        reconciliação
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

~~~
descobrir A
-> materializar A
-> Vision
-> ArmoredIA
-> Studio
-> Hub
-> confirmação
-> cleanup
-> descobrir B
~~~

Não existe pré-download de lote.

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

WAITING_VISION significa exclusivamente que a Vision V1 não conseguiu resolver o produto Shopee exato.

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
- localizar candidatos;
- materializar um único candidato;
- controlar checkpoints;
- administrar o lifecycle da sessão Telethon.

## 4.1 Regra de candidato

Um candidato válido contém vídeo e link Shopee na mesma mensagem, ou vídeo seguido imediatamente pela mensagem com o link Shopee.

Não existe busca arbitrária por links em várias mensagens seguintes.

## 4.2 CATCH-UP

O CATCH-UP histórico é progressivo e sequencial.

~~~
descobrir candidato
-> materializar
-> processar
-> confirmar
-> cleanup
-> próximo candidato
~~~

O checkpoint nunca pode saltar um predecessor que ainda não tenha conclusão segura.

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

~~~
1 chamada Gemini
-> até 10 candidatas
-> Policy local
-> primeira válida
~~~

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

Não se deve pré-criar uma publication que faça a pipeline tratar item novo como publicação já existente.

## 11.3 CONFIRMED

CONFIRMED exige mensagem real e message_id válido.

## 11.4 UNKNOWN

UNKNOWN significa evidência insuficiente.

~~~
UNKNOWN
-> RECOVERY
-> não republicar automaticamente
~~~

## 11.5 ABSENT

ABSENT exige evidência suficiente de ausência.

Uma operação externa potencialmente executada não pode ser tratada como inexistente apenas porque uma busca inicial não encontrou evidência.

## 11.6 Reconciliação

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

A suíte cobre arquitetura, Coordinator, Database, Storage, Sync, Recovery, Startup Audit, Telegram lifecycle, CATCH-UP, LIVE, reconnect, Vision V1, ArmoredIA, Policy, provider Gemini, Studio, RVC, FFmpeg, Hub, reconciliação, idempotência e cleanup.

Comando principal:

~~~
python -m pytest -q -W error::RuntimeWarning
~~~

Verificação adicional:

~~~
git diff --check
~~~

Última execução verde registrada antes das últimas alterações de observabilidade:

~~~
160 passed
1 skipped
~~~

As alterações de observabilidade devem passar novamente pela suíte antes do congelamento final.

---

# 18. Evidência operacional real em 29/09/2026

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

Esses dois casos comprovam integração real de Recovery, Vision, ArmoredIA, Studio, Hub, Telegram e cleanup.

Eles não substituem a prova específica de uma falha dentro da ArmoredIA seguida de Recovery direto na própria IA.

---

# 19. Auditoria real de startup em 29/09/2026

Resumo observado:

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

Publicações históricas ambíguas foram verificadas conforme o contrato do Hub.

---

# 20. Teste histórico do zero

O próximo teste operacional deve começar com o estado interno zerado, mantendo o histórico Telegram intacto.

## 20.1 O que é zerado

~~~
storage/database/armoredcreator.db
items = 0
publications = 0
checkpoints = 0
mode = CATCH_UP

storage/videos
vazio

storage/logs
vazio

storage/backups
vazio, preservando .gitkeep
~~~

## 20.2 O que não deve ser apagado

~~~
credentials/project.env
credentials/telegram/session/*
ArmoredStudio/runtime/rvc/*
modelos RVC
START_ALL.local-backup.bat
armoredcreator.db na raiz do projeto
~~~

O banco canônico é o que fica em storage/database/armoredcreator.db.

## 20.3 Resultado esperado

~~~
CATCH-UP
-> descobrir histórico
-> um candidato
-> materializar
-> Vision
-> ArmoredIA
-> Studio
-> Hub
-> CONFIRMED
-> PUBLISHED
-> cleanup
-> próximo candidato
~~~

## 20.4 O que observar

- um item ativo por vez;
- nenhum pré-download de lote;
- nenhum estado de fila física;
- WAITING_VISION somente para unresolved V1;
- RECOVERY para falhas técnicas;
- continuidade do histórico após falha;
- Recovery do estágio correto;
- checkpoint nunca saltando bloqueio;
- nenhuma republicação indevida;
- cleanup somente após confirmação;
- zero órfãos;
- CATCH-UP completo;
- entrada correta em LIVE.

---

# 21. CATCH-UP -> LIVE

Chegar ao fim do iterador não basta.

Condição:

~~~
histórico esgotado
+ nenhum bloqueio
+ itens elegíveis resolvidos
+ publicações confirmadas
+ cleanup concluído
+ zero órfãos
-> LIVE
~~~

Recovery pendente não pode ser ignorado para entrar em LIVE.

---

# 22. Vision V2

Vision V2 está fora desta versão.

Não fazem parte do fluxo atual discovery de equivalentes, pacote de múltiplos links ou CandidateDiscovery.

Uma futura V2 deve voltar isoladamente, com nova certificação.

---

# 23. Critérios de fechamento histórico

~~~
[ ] suíte final verde
[ ] diff --check limpo
[ ] SQLite zerado
[ ] checkpoints zerados
[ ] storage zerado
[ ] histórico Telegram intacto
[ ] CATCH-UP iniciado do zero
[ ] um item ativo por vez
[ ] nenhum pré-download
[ ] Vision V1 correto
[ ] WAITING_VISION somente unresolved real
[ ] ArmoredIA operacional
[ ] até 10 candidatas por chamada
[ ] Policy local
[ ] sem segunda chamada por exaustão de Policy
[ ] falha técnica de IA em RECOVERY
[ ] Recovery de IA sem rerun de Vision
[ ] Studio e RVC corretos
[ ] Hub idempotente
[ ] CONFIRMED com message_id real
[ ] UNKNOWN não republica
[ ] cleanup correto
[ ] checkpoints seguros
[ ] zero órfãos
[ ] CATCH-UP concluído
[ ] LIVE iniciado
~~~

---

# 24. Estado de certificação

## Fechado por contrato e testes

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
Studio
RVC
Hub
reconciliação
idempotência
cleanup
~~~

## Comprovado em execução real nesta fase

~~~
Vision -> ArmoredIA
Gemini real
Studio/RVC real
Hub real
Telegram CONFIRMED
PUBLISHED + cleanup
Recovery real
~~~

## Evidência ainda necessária

~~~
falha real de ArmoredIA + Recovery direto na IA
CATCH-UP histórico completo desde checkpoint zero
comportamento do histórico sob falhas reais
checkpoints históricos completos
transição final CATCH-UP -> LIVE
suíte verde após as alterações de observabilidade
~~~

---

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
-> primeira válida
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

---

# 26. Regra de manutenção

O baseline oficial continua intocado.

Qualquer mudança futura deve ser isolada em branch própria, passar pela suíte completa, passar por git diff --check e obter evidência operacional antes de ser tratada como fechada.