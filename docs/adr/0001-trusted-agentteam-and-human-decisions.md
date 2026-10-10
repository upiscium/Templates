# ADR-0001: Trusted AgentTeam と Human Decision / Ask の境界

- **Status:** Accepted (2026-10-10); implementation partial
- **Decision:** [#254](https://github.com/upiscium/Templates/issues/254)
- **Related:** #189, #194, #195, #215, #221

## Context

KUMIKI は、個人またはチームが**信頼された AgentTeam** を活用して効率的・安全にソフトウェアを開発するためのフレームワーク。KAGARI はその AgentTeam 実行支援を担う。過去の source publication / Admin 設計では、通常の誤操作防止と意図的な敵対行動への耐性が混同され、承認・隔離のコストが膨らんだ。

## Decision

1. 保証する安全性は、通常のミスや意図しない変更の抑制、作業と Git 履歴の保護、検証・独立レビュー、実際の postcondition 観測、異常時の停止・復旧可能性。**悪意ある Agent、意図的な権限濫用、root/管理者・ホスト侵害による制限回避への頑強性は保証しない**。
2. 正しい Task・worktree と必要な Guard を満たした通常の stage/add・commit・Task branch push・Draft PR 作成/更新・事実ベースの PR/Issue コメントは、AgentTeam が **AUTO** で実行してよい。安全性を満たした同一操作のたびに人間への重複 Ask、独立した承認者や外部署名を要求しない。
3. **Ask が必要な操作には必ず Ask する**。製品の方向、要件、アーキテクチャ、意味的な競合、他者の作業・履歴の不可逆な破棄、デフォルトブランチへの最終 Merge などを Agent が勝手に決めない。Ask には対象と選択肢・影響・既知の証拠を示す。
4. Guarded API の technical precondition と Human decision / authority は別の責務。Guard が PASS でも製品判断の承認とはならない。逆に Human Ask がないからといって、正常な機械的 Task 操作を人為的に止めない。
5. Git/GitHub/実ファイルの事実を正本とし、操作後・中断後は再観測する。不一致を全部停止にせず、安全に解決できる機械的相違は自動収束させる。意味・所有権の不明な衝突は保全して Ask。
6. 独立 Ed25519 承認署名、暗号学的 Approval A/B 儀式や独自の複雑な GitHub 承認基盤は通常の trusted publication の必須要件としない。実際の誤操作を防ぐ価値が明確でない追加機構は採用しない。

## Consequences / boundary

- 最終 Merge には Human 承認が必要。Draft PR / CI Green / Arca READY / 単なる実装への賛成を Merge 許可と混同しない。
- リポジトリローカルの Guarded API と OpenCode permission / role 設定は別途整合させる。**現行配布 v3 には `just agent::push *: ask` が残る**が、これは v4 の自律 Push 方針と一致しない移行課題。
- この ADR は直接 Push / 強制変更 / Merge を承認しない。安全機構の変更は別の実装・検証 PR とする。
