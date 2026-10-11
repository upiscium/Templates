# ADR-0002: 着脱可能で再導入できる KAGARI と Project 所有権

- **Status:** Accepted (2026-10-10); implementation pending
- **Decision:** [#254](https://github.com/upiscium/Templates/issues/254)
- **Related:** #142, #146, #159, #194, #242, #26

## Context

KAGARI は既存・新規の Git repository に必要なとき導入でき、不要になったら削除できるべきである。現行の採用・Admin engine には install / replace / uninstall / upgrade があるが、既存の Project `Justfile`・`flake.nix`・`opencode.json` などとの競合や、破損状態からの再導入という契約は未完成である。

## Decision

**K1 — Detachable component.** KAGARI は Project と独立した *optional* component とし、`install` / `uninstall` / `reinstall`（及び実装上の `replace`）を標準操作として提供する。ユーザーの製品コードや開発ツールの所有権を奪わない。

**K2 — Bootstrap independence.** Install / Repair / Uninstall の入口は、対象 Project の `flake.nix`、`Justfile`、壊れた `.automation/**`、既存 KAGARI ランタイムに依存しない。Nix・Just・OpenCode が未導入の対象でも、外部提供の bootstrap / toolchain を使える経路を設ける。具体的な CLI 名、Nix installable、OpenCode 設定の優先順位は実装前に検証して選ぶ。

**K3 — Project-first ownership.** `flake.nix` / `flake.lock` / `Justfile` は Project 所有。標準導入はこれらを必須にせず、無い場合も勝手に新規作成しない。存在する場合も勝手に編集しない。KAGARI 固有の依存・Just recipe・設定は別管理領域とし、任意で統合する場合だけ明示的な差分と可逆性を提示する。プロジェクト固有のパッケージ追加・Just 拡張は通常 Task で行えるべきであり、丸ごと固定してはならない。

**K4 — Idempotent reinstall/repair.** 導入済み・部分欠落・一部破損の KAGARI について、既知の管理対象を照合し、同じ `install` / `repair` 経路で修復・再導入できるようにする。破損時に `uninstall` を先行必須にしない。既存 Project ファイルや他人の変更、Git 履歴、Task 記録・Evidence を無断で削除・上書きしない。既知でない ownership / user edit との競合は診断し、必要時のみ Human Ask。uninstall は KAGARI が所有するインストール対象を外し、Product/Task データ削除は別の明示的操作とする。

## Integration matrix (normative default)

| Project state | Default KAGARI behavior |
| --- | --- |
| `flake.nix` present | そのまま保持。読み込み・修正は optional |
| `flake.nix` absent or broken | Project flake を起動前提にしない |
| `just` present / absent | 対応版なら利用、無い場合は外部実行経路 |
| `Justfile` present | 既存内容を保持。KAGARI recipe は別 file/optional routing |
| `Justfile` absent | デフォルトでは作成しない |
| `opencode.json` / `AGENTS.md` present | 競合を検査し、標準で無断上書きしない |
| `.automation/**` partial/corrupt | 既知の KAGARI-owned payload を照合して修復 |
| unknown modified managed file | 保全して診断。消す/採用する判断が必要なら Ask |

Nix `import` による KAGARI 管理モジュールと Project 設定の分離は有効な選択肢。ただし import 元 `flake.nix` 自体を改変不能にする保証ではない。既存 Justfile を編集しない別 `--justfile` 利用も候補であり、実機検証後に採用を決める。

## Acceptance evidence required before calling this implemented

既存/不在/破損の `flake.nix`・Justfile・Just CLI・OpenCode 設定の組合せで、**Install → 動作 → Uninstall → Reinstall/Repair** の実機 smoke を実施する。対象 Project の所有ファイルが byte-identical に保たれ、無関係の staged/dirty data や Git history が保持されることを検証する。新しい KAGARI CLI が既に存在するという主張ではない。
