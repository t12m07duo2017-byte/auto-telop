# customers/_template/

新規顧客フォルダの雛形。`auto-telop new-customer <顧客名>` を実行すると、この中身が
`customers/<顧客名>/` へ自動コピーされ、ライセンスキー(`license.key`)も追加で
生成・配置される。

新規顧客追加の完全な手順は `docs/新規顧客追加手順.md` を参照。

`customers/<顧客名>/` に最終的に揃えるべき3点セット:

| ファイル | 用途 | 作成方法 |
|---|---|---|
| `template.prproj` | そのお客さん用に作り込んだPremiereテンプレ(Essential Graphicsスタイル込み) | 開発者が手作業で作成 |
| `style_analysis.json` | テンプレから抽出したスタイル名/SE候補一覧 | `auto-telop analyze-template --template template.prproj -o style_analysis.json` |
| `style_se_categories.json` | カテゴリ→スタイル→SEの対応表 | このテンプレをコピーし、`style_analysis.json`を見ながら手入力 |

これに加えて `license.key`(実行許可用の署名付きトークン、`new-customer`実行時に自動生成)。

このフォルダ(`customers/_template/`)自体は実データを含まないため、公開リポジトリに
含めてよい。`customers/<顧客名>/`(実データ)は`.gitignore`で除外されており、
**絶対にコミットしないこと**。
