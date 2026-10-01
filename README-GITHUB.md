# 無料GitHub版 v0.2

公開リポジトリ＋標準Ubuntuランナー＋GitHub Pagesで動かす版です。VPS・有料API・外部DBの契約は不要。実行先のリポジトリは未指定で、現時点ではGitHub上に設置していません。

## 最初にすること（スマホからでも可）

1. GitHubで専用の新しい **Public（公開）** リポジトリを作る。名前の例は `boat-data-ai`。競馬など既存のアプリと分ける。
2. このZIPを解凍し、`boat-foundation`フォルダの**中身**をリポジトリの直下へアップロードする。フォルダごと1段下へ入れるとActionsを認識しない。`.github/workflows/collect.yml`も含める。
3. リポジトリのActionsで `Boat data (free public repository only)` → `Run workflow` を1回実行する。初回は付属データを読み込み、前7日＋今日の取得を試すので時間がかかる。
4. 初回の処理後、Settings → Pages → Sourceを **Deploy from a branch**、Branchを **gh-pages**、フォルダを **/ (root)** にしてSaveする。
5. Pages画面に表示されるURLをスマホで開く。以後は定期更新する。Pages設定がない初回でも、収集データと画面用ブランチを先に保存する。

アップロード先URLを共有すれば、接続できるリポジトリに対して配置・設定確認を進められます。現時点で接続先一覧は空のため、勝手に既存アプリを変更していません。

## 公開と費用の前提

- GitHub公式の説明では、公開リポジトリで標準GitHubホストランナーを使う処理は無料。larger runnerは使わない。
- 非公開リポジトリの場合、収集ジョブをスキップするガードを付けた。実行スクリプトでも公開状態を検証し、非公開なら止める。
- 定期処理にはActionsのアップロード成果物・キャッシュ・Git LFS・GitHub Packagesを使わない。料金対象となり得るこれらの保存枠に収集データを蓄積しない。
- 画面は `gh-pages`、DBとRAWはReleasesに保存する。GitHub Pagesのビルドは公式APIで要求する。
- **コード・データ・画面は公開される。** この版は公開競艇データの収集基盤で、個人的なメモ・購入記録は付属していない。今後の機能の公開範囲は別途決める。
- このプログラム以外の既存GitHub利用・有料プラン・支払い設定を変更するものではない。GitHubの料金/利用規約が変わる場合は構成を見直す。

## 実行頻度

毎日JST **08:07〜23:07に1時間ごと**、加えて **01:07** に結果を拾う。通常は前3日＋今日を再取得し、後日の訂正を取り込む。初回のみ前7日＋今日。

GitHubの定期実行は遅延・スキップする場合がある。締切直前に予想を届ける常時サービスではない。元CSVが保存している締切前オッズも、こちらで取得するまで利用できていた扱いにしない。

手動で追加更新する場合はActionsのRun workflow。無料版にdaemonの無限ループを入れない。

## 消えないための保存構成

| 場所 | 内容 |
|---|---|
| デフォルトブランチ | Pythonコード、Actions設定、初期データZIP |
| `gh-pages` | カルテ・取得状況・最終更新時刻の画面 |
| Release `boat-state` | SQLiteの直近3世代と、RAW保存先を記した確定マニフェスト |
| Release `boat-raw-YYYY-MM` | 新規RAWだけを追加したZIP群。自動削除しない |

処理順は **前回DB復元 → 収集 → 新規RAW保存 → DB保存 → マニフェスト確定 → 画面更新**。

新しいDBを全部保存した後にマニフェストを最後に追加する。中断時は以前の確定マニフェストを使う。チェックサム不一致や欠けたDBを検出したら処理を止め、空DBで再開しない。元RAWは別の月別Releaseに追加し続ける。

古い3世代以前のDBコピーと、再計算できるカルテ出力の履歴だけを整理する。レースの取得履歴・予測固定テーブル・RAW本体は削除しない。ZIP内の初期データは、今回確認した出走表456レース・確定結果288レースを含む。

GitHubの保存容量・ファイル数・サービス継続には限界があるので、無期限の保存を保証する表現はしない。現状のガードは月別RAWのReleaseが990ファイルに近づいた場合、ZIPが1800MiBを超えた場合、画面が20MiBを超えた場合に明示的に止める。課金プランへの自動切替はしない。

## 画面更新

画面上の最終更新時刻を確認する。最新の計算と、締切時点に利用可能だった情報は区別する。カルテは数値を中心に表示し、画面内の直近実績は各5走までに軽量化した。DBには履歴を保持する。

既に`gh-pages`に別のサイトがある場合、この処理は置き換えずエラーにする。専用リポジトリを使う。

## 停止・復旧

- 止める：ActionsのワークフローをDisable workflow。保存済みReleasesは停止だけでは消えない。
- 再開する：Enable workflow、続いてRun workflow。
- 長期間リポジトリに活動がないと定期処理が無効になることがある。GitHubは公開リポジトリで60日間活動がない場合の停止を案内している。自動実行だけで永久に無効化を防げるとは扱わない。
- 途中失敗：次の定期処理は前の確定DBから再取得する。初回のDBアップロードだけ完了しマニフェストが無い場合は、孤立アップロードを検出して停止する。ActionsログとReleasesを確認して復旧する。自動削除・自動初期化はしない。
- GitHubの削除・障害に備えるにはReleasesのDBとRAWの手元ダウンロードも必要。予測AI搭載前に復元手順を実際のリポジトリでも試す。

Pythonと認証済みGitHub CLIがあるPCで全RAWまで復元する例：

```sh
python - <<'PY'
from boatdata.github_state import GitHub, restore
restore(GitHub('YOUR_NAME/boat-data-ai'), 'restored-data', with_raw=True)
PY
python -m boatdata --data restored-data report --date 2026-10-01
```

## 検証済み・未検証

36件の自動テストに合格。取得元時刻、後日取得、F/L・欠場、RAW、訂正、DB復元に加え、遠隔保存の途中失敗、チェックサム不一致、RAWの差分保存、古いDB世代の整理をテスト。

Pages用Gitブランチの初回作成・次回更新・別サイトを保護する動作は、ローカルのGitリポジトリを使って確認した。実データの保存/復元と画面生成もローカルで確認する。

**実GitHubのReleases権限・定期実行・Pagesビルド完了は、接続先が未指定のため未検証。** 予測モデルもまだ搭載していない。

## GitHub公式資料

- Actionsの料金: https://docs.github.com/en/billing/concepts/product-billing/github-actions
- 定期実行の仕様: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- Releases: https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases
- Pages API: https://docs.github.com/en/rest/pages/pages
