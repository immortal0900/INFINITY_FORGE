---
name: memex
description: "MEMEX 개인 지식 DB 사용 규율: 읽기는 search_* 소프트 의존(실패해도 진행), 쓰기는 배치 1회 또는 outbox 경유, 위키는 MEMEX wiki 일원화. 에러 해결·결정·교훈이 생겼을 때, 또는 과거 지식이 필요할 때 사용."
version: 1.1.0
author: INFINITY_FORGE
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Memex, Knowledge, Memory, Neo4j, Wiki]
    related_skills: [forge-ops]
---

# MEMEX 지식 DB 규율

MEMEX MCP 서버(도구: save_memex, save_source, search_all, search_errors, search_qa, search_insight, search_decision, wiki_ingest, wiki_query)가 연결되어 있다.

## 읽기 (soft 의존 — 절대 작업을 막지 않는다)
- 에러를 만나면 먼저 search_errors로 과거 해결 사례를 찾는다. 설계 결정 전에는 search_decision을 확인한다.
- MEMEX 호출이 실패하거나 느리면(2~3초) 그냥 진행한다. MEMEX 없이도 작업은 완결되어야 한다.

## 쓰기 (쿼터 절약 — save 1건 = LLM 최대 3회 소모)
- 저장 시점: (a) 작업/spec이 끝났을 때 배치 1회로 몰아서, (b) 실패의 교훈 = [error], (c) 확정된 결정 = [decision], (d) 재사용 가치 = [qa] 또는 [insight].
- 작업 중간에 건건이 저장하지 않는다. 긴급하지 않으면 outbox 디렉토리(리눅스 `~/forge/outbox/`, Windows `%USERPROFILE%\forge\outbox\`)에 md 파일로 적재만 하고 진행한다 (flush 스크립트가 배달, fire-and-forget).
- outbox 파일 형식: `## [aspect] 제목` 헤더 + `project::` + `tags::` + `recorded_at::` 필드 (aspect = error|decision|qa|insight).

## 위키 (2026-07-10 결정: MEMEX 일원화)
- 위키가 필요하면 **MEMEX의 wiki_ingest / wiki_query만** 사용한다.
- hermes 번들 llm-wiki 스킬(로컬 md 위키)은 **사용하지 않는다** — 위키가 둘이면 지식이 분산되고 서로 어긋난다. 이 결정의 변경은 인간 승인 필요.

## 금지
- 대화 Transcript 원문 저장 금지 (요약만). 스킬 본체 파일 저장 금지. API 키·토큰 저장 금지.
- 작업 진행상태(카드 상태 등)는 MEMEX에 쓰지 않는다 — kanban 원장의 몫. MEMEX는 지식 전용.
