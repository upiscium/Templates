# ADR-0003: 自己更新可能な source 開発と統一した例外・復旧

- **Status:** Accepted principles (2026-10-10); concrete source-guard implementation choices pending
- **Decision:** [#254](https://github.com/upiscium/Templates/issues/254)
- **Related:** #140, #142, #146, #189, #198, #215, #219, #220, #221 / PR #224, #228, #250

## Context

KUMIKI と KAGARI が自分自身を継続的に改良できないと、開発基盤そのものが停止する。現行 KUMIKI source 専用の `templates-source` は `publication-check` / `commit` / `push` / `pr-create` / `checkpoint` を提供する一方、固定 launcher の自己更新を防ぐため7つの source path を保護し、変更履歴に現れただけでも拒否する。これにより正常な `flake.nix` の更新などが特例 bootstrap に回る。生成テンプレートには `templates-source` は含まれず、この問題を生成先 KAGARI と混同しない。

## Decisions (principles)

1. **Self-hostability**: KUMIKI と KAGARI のソース編集・Commit・Task branch Push・Draft PR は通常の Guarded 開発フローで行えるべき。編集すること、レビュー済み変更を公開すること、変更後の Guard/runtime を有効化することは異なる操作であり、混同しない。
2. **Narrow, explainable protection**: Guard が実行中に用いる固定コード・設定と、開発中の Project/Guard ソースを分ける。正常な Project `flake.nix`/Justfile の修正・依存追加が恒久的に阻止されてはならない。`flake.nix` から別の KAGARI-owned .nix ファイルの `import` は候補であり、root `flake.nix` を不変にすることは目的にしない。
3. **Standard paths, no per-incident gate proliferation**: 自己更新、Install、Replace、Uninstall、Repair、通常 publication、障害後の安全な再観測を、それぞれ明示した共通の operation / recovery 契約へ統合する。例外のたびに署名者管理や新しい一回限りの Publication Gate を増やさない。
4. **Unknown failure rule**: (a)当該操作の追加変更をいったん停止、(b)Git/GitHub/FS の実状態を読み取り、(c)既存作業・履歴・Evidence を保存、(d)安全性が証明できる収束・再導入は自律実行、(e)意味・所有権・不可逆性に不明点があれば具体的な Decision View と Human Ask、(f)通常ランタイムが破損している場合はそれに依存しない共通 Repair/Bootstrap 経路から復帰。停止が必要でも次の明示的な安全経路を示す。
5. **Proportional authorization**: Guarded routine Commit/Push の追加 Ask・独立署名は要らない。人間の判断が必要な Merge・意味的な衝突・破棄などの Ask は維持。意図的な悪意ある Agent / 特権主体の攻撃への耐性は範囲外。
6. **#224 compatibility**: #221 / Draft PR #224 の signed Approval A/B と独立 Ed25519 signer は trusted source publication の必須条件から外す。現在の local gate source はまだ signing 必須の実装候補であり、**実装修正・再レビュー・統合は未完了**。残存 main race は合意済みで、任意の回復モードは #250 の非ブロッキング議論に分離する。

## Existing baseline — do not misreport as completed

`tools/source_collaboration.py` の `SOURCE_AUTHORITY` は現在 `Justfile`、`just/source.just`、`tools/source_collaboration.py`、`tools/source_publication_launcher.sh`、`just/template.just`、`tools/render_templates.py`、`flake.nix` の7パス。現行 `just source::* ` は意図的に inert。固定 `templates-source` は Nix Store 上の別コードで、正しく Guard された通常 Task branch 操作を提供する。これを無断で無効化・置換しない。

現在の Admin / install・replace・uninstall・cutover の実装は段階的開発中で、KAGARI v4 の本番利用を証明しない。`flake.lock` や別のテンプレート配布プログラムはこの7パスの保護セットに含まれないため、現在の7パスが完全な安全性分類だとは主張しない。

## Follow-up design work (not already decided)

固定 launcher を維持/簡素化/置換する方式、最小の本当の protected scope、Guard の正常な有効化手順、共通 Recovery API の具体的な形式、既存 Project の OpenCode configuration の正確な合成方法は、現状調査と positive/negative test から決める。これらの実装方式は本 ADR の確定事項とは区別する。

この ADR は現行 #224 の Publication 承認、既存 PR の Ready / Merge 許可、署名なしで旧 Gate を迂回してよいという宣言ではない。
