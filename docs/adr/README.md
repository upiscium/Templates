# Architecture Decision Records

KUMIKI（組木）/ KAGARI（篝）の合意済み設計判断を記録します。採択された**設計方針**と、現在利用できる**実装・機能**は区別してください。

| ADR | Status | 対象 |
| --- | --- | --- |
| [0001 — Trusted AgentTeam と Ask 境界](0001-trusted-agentteam-and-human-decisions.md) | Accepted; implementation partial | 目的・安全性・自律操作・人間の判断 |
| [0002 — 着脱可能な KAGARI と Project 所有権](0002-detachable-kagari-and-project-ownership.md) | Accepted; implementation pending | Install/Uninstall/Reinstall、Nix/Just/OpenCode、Repair |
| [0003 — 自己更新可能な source 開発と統一復旧](0003-self-hosted-development-and-recovery.md) | Accepted principles; implementation choices pending | Source Guard、自律更新、既知・未知の障害への対処 |

**Decision tracking:** [Issue #254](https://github.com/upiscium/Templates/issues/254)。既存の v4 [Issue #189](https://github.com/upiscium/Templates/issues/189)、KUMIKI/KAGARI 改名 [#242](https://github.com/upiscium/Templates/issues/242)、#221 / Draft PR #224、#250 と整合させる。

この ADR 群は、新しい bootstrap launcher や CLI がすでに配布されているという宣言ではありません。現行の生成先 Agent Core は **VERSION 3** で、`components/agent-core-v4/` は段階的に実装されていても本番適用とは別です。

古い Issue・設計文書・テストは履歴証拠として保持します。矛盾がある場合は決定の前後関係・適用範囲を記録し、無断で既存の Guard や権限を変更しません。
