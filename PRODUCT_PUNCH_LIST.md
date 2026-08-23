# STARRYPAD 商用ソフト比較・改善パンチリスト

調査日: 2026-08-23

## 1. 調査の目的

機能数を商用ソフトと揃えることが目的ではない。STARRYPAD MINIをつないだ人が、次の成果を短時間で得られるかを基準に差分を評価する。

1. 何も調整せず、叩いた強さとタイミングが意図どおり音になる。
2. 録音操作を忘れても、良かった演奏を失わない。
3. ミスだけ直し、演奏のニュアンスは残せる。
4. 録った音をすぐ演奏可能な音にできる。
5. 1ループを展開し、曲またはライブ演奏へ進められる。
6. 壊す不安なく試し、元に戻せる。
7. DAWや他人へ、再利用しやすい形で渡せる。

## 2. 比較対象から得た設計原則

- Koala Samplerは`Sample / Sequence / Perform`を分離し、波形編集、シーケンス編集、ライブFXを必要な場面でだけ見せる。小さなUIで機能の深さを保つ参考になる。
- Maschineはアイデア作成用のPatterns/Scenesと、完成へ進めるSong viewを分けている。サンプル録音はシーケンサーを止めずに行え、編集、スライス、レイヤーへ連続して進める。
- MPC 3はDirect to Pad Sampling、Drum Grid、Pad Mixer、8 Sample Layers、Linear Arrangerを一つのプロジェクトに統合する。さらにカウントイン、ブラウザ試聴、トラック別書き出しを備える。
- Ableton Live/PushはCapture MIDIで録音前の演奏を回収でき、Simpler/Drum Rackで波形編集、スライス、Warp、チョーク、センドを提供する。MIDI編集ではベロシティ、発音確率、量を指定できるクオンタイズを扱える。
- Serato StudioはPad単位のAttack/Release、Mono/Poly、Tempo、Key Shiftから、Scenes、Song View、Automation、Stem/Pad別書き出しまでを段階的に開く。
- Superior Drummer 3とAddictive Drums 2は、パッド/楽器ごとのベロシティカーブ、幅広い強弱レイヤー、交互サンプル、詳細なハイハット応答を「演奏して自然に聞こえる」ための基礎としている。

## 3. 現状の強み

以下は維持すべきであり、商用ソフトとの差を埋める過程で後退させない。

- STARRYPAD MINI向けの物理配置、DONNER/GM/Learnマッピング、16パッド同時発音。
- 専用オーディオスレッド、WASAPI、小バッファ、96音チャンネルによる低遅延志向。
- ベロシティレイヤー、パッド別Sensitivity、ハイハットのチョーク。
- 4 Kit、内蔵121サンプル、カスタムサンプル録音/ドラッグ&ドロップ。
- Note Repeat、メトロノーム、Tap Tempo、1/2/4小節ルーパー、Overdub、Undo、Quantize。
- WAV/MIDI書き出し。
- MIDI番号、診断値、入力デバイスなどを演奏面からSettingsへ移した情報設計。

## 4. パンチリスト

### P0: 「叩けば正しく鳴る」と「演奏を失わない」

#### P0-0 内蔵サンプルのアタック位置QA

- 状態: 完了。弱打スネア4候補を維持したまま遅延素材を補正し、自動回帰テストを追加した。
- ユーザー成果: 同じタイミングで叩いた音が、強弱やランダム音色によって前後せず、リズムが安定して聞こえる。
- 現在の摩擦: 弱打スネア候補の`snare2_OH_Ghost_4.wav`だけ発音開始が約45.6 ms遅く、ランダム選択時に約4回に1回の割合でスネアが遅れて聞こえる。
- 手段: 元の音源を保持したまま、再生用コピーの先頭余白を補正する。Kitへ追加する全サンプルにアタック位置の自動測定を行い、同一レイヤー内の開始差を基準内に収める。
- UI/UX: 補正設定や数値は画面へ追加しない。音色の自然なランダム変化は残し、タイミングだけを安定させる。
- 完了判定: 弱打スネア全候補の検出アタック差が3 ms以内で、同一間隔の連打を行っても素材起因の遅れを知覚しない。将来追加するハイハット、ライド、スネアの各4音色にも同じQAを適用する。

#### P0-1 パッド・キャリブレーションウィザード

- 状態: 完了。選択した物理Padを弱・通常・強で各3回叩くSettings内ウィザード、中央値によるPad別3点カーブ、強打Velocity 116目標、保存・初期化を実装した。Calibration中は35 ms内のraw pulseを演奏Hitから除外し、観測した最大二重発火間隔+2 msを6～30 msへ収めてPad別Dead Timeとして自動保存する。初めてMIDI接続できた時だけSettingsでのCalibrationを案内し、以降の起動では繰り返さない。
- ユーザー成果: 弱打、通常打、強打が、自分の力加減どおりの音量と音色になる。
- 現在の摩擦: パッド別Sensitivityは倍率だけで、入力下限、最大値、カーブ、二重発火を個別に補正できない。
- 手段: 各パッドを`弱く3回 / 普通に3回 / 強く3回`叩いて入力分布を取得し、入力下限、上限、3点カーブ、デッドタイムを自動設定する。後からカーブだけ微調整可能にする。
- UI/UX: 初回接続時に一度だけ提案し、普段はSettings > Pad setupへ隠す。数値より`Soft / Natural / Hard`の試打結果を先に見せる。
- 完了判定: 弱打を取りこぼさず、通常打が急に最大音にならず、強打の95%以上が出力Velocity 115以上になる。高速ロールで不要な二重発火がない。

#### P0-2 自動再接続と「音が出ない」復旧導線

- 状態: 完了。2秒間隔の切断検知、保持音停止、同一MIDI機器だけの自動再接続、ミキサー停止または選択出力消失時のWASAPI再初期化と既定出力への復旧を追加した。UI heartbeatが8秒以上飛んだ場合はSleep復帰としてPanic、Metronome/Clock deadline初期化、MIDI/Audio健全性確認を同じframeで強制する。正常時は小さなConnected表示だけを維持し、異常時のみ赤いReconnectボタンを出す。疑似長時間Sleep試験で待機中の2秒pollを飛ばし、両接続確認とAll Sounds Offが即時発火することを固定した。
- ユーザー成果: USBの抜き差し、スリープ復帰、出力デバイス変更後も演奏へすぐ戻れる。
- 現在の摩擦: MIDI/オーディオ障害時の復旧がSettingsのデバイス切替頼みで、演奏者は原因を判断しにくい。
- 手段: MIDIホットプラグ監視、自動再接続、オーディオ再初期化、All Sounds Offを実装する。失敗時だけヘッダーの接続表示を操作可能にし、`Reconnect`を一手で実行する。
- UI/UX: 正常時は今の小さなConnected表示のまま。異常時のみ赤い状態表示と復旧ボタンを出し、ログやドライバー名はSettingsに残す。
- 完了判定: 演奏中の抜き差しとスリープ復帰から5秒以内に自動復帰し、鳴りっぱなしの音が残らない。

#### P0-3 オーディオ出力・遅延セットアップ

- 状態: 完了。Settings内に出力デバイスと`Low latency / Stable`を追加し、Advancedで48/44.1 kHzと64/128/256 samplesを選べる。変更時は音声ワーカー停止、ミキサー再初期化、全Sound再読込を行い、失敗時は直前設定へ戻す。`Test 10 sec`は16Padを125 ms間隔で81回鳴らし、Mixer停止、Audio例外、Queue詰まり、trigger p99から`Low latency passed / Use Stable mode`だけを返す。実機Windows既定出力48 kHz/128 samplesで例外0、Queue 0、p95 0.286 ms、p99 0.441 msを確認した。打撃時Pitch resampleを除去し、固定Tuneを事前生成して初回Hit遅延を解消した。SDL2/Pygame経路に実在しないWASAPI排他切替は表示せず、共有Low latencyを検証対象とした。
- ユーザー成果: 音切れせず、叩いてから聞こえるまでの遅れを最小にできる。
- 現在の摩擦: 出力、ドライバー、バッファが実質固定で、環境によって音切れまたは過大な遅延が起きても調整できない。
- 手段: Settingsに出力デバイス、48/44.1 kHz、64/128/256 samples、共有/排他を置く。10秒の試打テストでドロップアウトを検知し、最小の安定設定を推奨する。
- UI/UX: 通常画面には数値を出さない。`Low latency / Stable`の2プリセットを主操作にし、詳細値はAdvanced内に置く。
- 完了判定: 推奨設定テストでドロップアウト0、p99トリガー処理時間が基準内、設定変更失敗時に直前の動作設定へ戻る。

#### P0-4 プロジェクト、Autosave、Crash Recovery

- 状態: 完了。アプリ設定と`.starrypad.json` Projectを分離し、既存データを自動移行する。Kit、Pad編集、Custom Sample参照、Loop、Tempoを操作ごとに原子的Autosaveし、最新バックアップから自動復旧する。ヘッダーのProject名からNew/Open/Recent/Save As/Collect Samplesを実行でき、`Project.samples`フォルダとProjectファイルを一緒に移動できる。
- ユーザー成果: Kit、サンプル、ループを曲ごとに保存し、クラッシュや誤終了でも失わない。
- 現在の摩擦: 1つのsettings JSONがアプリ設定と制作内容を兼ね、複数曲、名前付き保存、復旧、サンプル収集がない。
- 手段: アプリ設定とProjectを分離する。ProjectにはKit、パッド編集、サンプル参照、パターン、テンポを保存し、Autosave、Recent、Save As、Collect Samplesを実装する。
- UI/UX: ヘッダーにはプロジェクト名と未保存ドットだけ表示する。保存操作は自動を基本にし、終了確認を常態化させない。
- 完了判定: 強制終了後に直前30秒以内の状態を復元でき、別PCへProjectフォルダを移してもサンプル欠落なしで開ける。

#### P0-5 全体Undo/Redoと破壊操作の安全化

- 状態: 完了。LoopのRecord、Overdub、Clear、Quantize、Bars、Captureに加え、Pad音色、Sensitivity、Mixer、Custom Sample割当/解除、非破壊Waveform編集、Tempo/Stretch、Auto Chop、Browser置換、Kit、BPMを20手のProject Undo/Redoへ統合した。Auto Chopは全Pad展開を一手で戻し、Project編集のUndo中はLoop再生位相を維持する。
- ユーザー成果: 音交換、録音、Clear、Quantizeを恐れず試せる。
- 現在の摩擦: Undoはループイベントだけで、サンプル上書き、Kit変更、Clear、感度変更は戻せない。
- 手段: Command履歴を導入し、Project内の編集をUndo/Redo対象にする。録音開始時の既存サンプルは一時保持し、Clearは即時実行してもUndo可能にする。
- UI/UX: 演奏を止める確認ダイアログは極力使わない。実行後に短い`Cleared - Undo`通知を出す。完全削除だけ確認する。
- 完了判定: 主要編集を最低20手戻せ、Redoでき、Undo中も再生が止まらない。

#### P0-6 録音のCount-in、次小節開始、Replace/Overdubの明確化

- 状態: 完了。Settingsで`Count 1 bar / Next bar / Instant`を選べる。待機中は旧ループを保持し、開始時だけ置換する。待機中のRecord再押下は安全にキャンセルし、画面へ残り拍を表示する。
- ユーザー成果: 最初の一打を欠かさず、狙った小節頭から正確に録れる。
- 現在の摩擦: Record直後に始まるため、マウスからパッドへ手を移す時間が演奏に混ざる。RecordとOverdubの関係も表示だけでは予測しにくい。
- 手段: `今すぐ / 次の小節 / 1小節カウント`を追加し、初期値は1小節カウントにする。Replace、Overdub、停止後再生の遷移を明示的な状態機械にする。
- UI/UX: Record押下後はボタンを点滅させ、タイムラインに残り拍を大きく表示する。設定はRecord横の小さなメニューへ置く。
- 完了判定: 1拍目が欠落せず、各録音モードの結果を初見ユーザーが試行3回以内に予測できる。

#### P0-7 Capture Last Performance

- 状態: 完了。音声を常時保存せず、最大120秒のMIDI打点だけを保持する。最後の2秒以上の休止以降を1/2/4小節へ収め、即時再生し、Undo/Redoで戻せる。
- ユーザー成果: 録音を押していないときに出た良い演奏を取り戻せる。
- 現在の摩擦: パッド入力は鳴るだけで、Record前の演奏は消える。
- 手段: 直近30～120秒のMIDIイベントをリングバッファに保持し、`Capture`でテンポ推定または現在BPMに合わせて新規パターンへ変換する。
- UI/UX: 常時録音を示す赤表示は出さない。演奏があるときだけCaptureアイコンを有効にし、押した後に結果を聴かせる。
- 完了判定: Recordなしで2小節叩き、Capture一回で再生可能になる。音声は保持せずMIDIイベントだけなので負荷とプライバシー影響が小さい。

#### P0-8 サンプリング開始・終了の失敗を減らす

- 状態: 完了。Auto/Manual開始、200 msプリロール、信号Threshold開始、1.2秒Silence Stop、入力Level、clip検知を実装した。SettingsのMonitorは初期値Offで録音中だけ70%音量のduplex監視を行い、非対応環境ではInput-onlyへ自動Fallbackする。Multiは処理完了ごとに次の未使用Padへ進み、満杯または処理失敗で停止する。clip時はPadへ割り当てる前に`Record Again / Keep Anyway`を表示し、判断中はMultiを進めない。10回のAuto Start回帰試験でpre-roll内のattack欠落0、無音録音は既存QAで拒否する。
- ユーザー成果: 先頭が切れず、無音やクリップを含まない使えるサンプルを一度で録れる。
- 現在の摩擦: 手動開始/停止中心で、録音前レベル、クリップ、入力監視、しきい値開始、プリロールが不足する。
- 手段: 入力レベルを録音前から表示し、100～300 msプリロール、Threshold Start、Silence Stop、クリップ検知、Monitorを追加する。連続して空きパッドへ録音するモードも用意する。
- UI/UX: Pad選択後のSampleモードでだけ波形と入力メーターを展開する。通常のPerform面には録音ボタン以外を残さない。
- 完了判定: 打音テスト10回でアタック欠落0、無音サンプル0、クリップ時は保存前に再録音を選べる。

### P1: 「素材を自分の音にする」と「ループを育てる」

#### P1-1 非破壊Waveform Editorと再生モード

- 状態: 完了。Custom SampleごとのStart/End Crop、Zoom、Normalize、Reverse、Attack/Release Fade、Tune、One-shot/Gate/Toggle/LoopをProject内の編集値として保存し、元WAVを変更しない。選択Pad専用の波形モーダルでPlayと一押しA/B、Reset、全体Undoを利用できる。Gate/LoopはMIDI Note OffとReleaseへ追従し、Toggleは再打で停止する。Loop/Toggleは境界へ最低3 msのFadeを適用し、編集結果はライブ再生とMaster/Stem/Bundle書き出しへ共通反映する。
- ユーザー成果: 録った音の不要部分を切り、意図した鳴り方にできる。
- 現在の摩擦: 自動Trim/Normalize/Fade後の結果を視覚確認・再編集できず、One-shot以外の挙動を選べない。
- 手段: Start/End、Zoom、Crop、Normalize、Fade、Reverse、Attack/Release、Tune、One-shot/Gate/Toggle/Loopを追加する。編集値は元WAVに焼かずProjectへ保存する。
- UI/UX: Sampleモードの選択パッドにだけ波形を表示し、頻用4操作を表、残りをToolsメニューへ置く。変更前後のA/Bを一押しで比較可能にする。
- 完了判定: 元ファイルを壊さず編集を戻せ、ループ境界でクリックせず、パッドを離したときの挙動を3モードから選べる。

#### P1-2 Step/Grid Editor

- 状態: 完了。Perform/Sequenceビューを分離し、Sequenceに16行×16ステップを実装した。1/2/4小節は1小節単位でページ移動でき、再生中の追加・削除、イベント選択、Velocity、±5 ms Nudge、次ステップCopy、Probability 0～100%、Ratchet x1～x4、Shift範囲選択、Undo/Redo、再生ヘッド表示に対応する。Step Input有効時は物理PadのVelocityを現在カーソルへ記録して自動前進する。Probability/RatchetはProject、ライブ再生、MIDI/WAV Exportで再現される。
- ユーザー成果: 間違えた一打だけ直し、手演奏では難しい反復も作れる。
- 現在の摩擦: 下部タイムラインは全パッドの位置を重ねて表示するだけで、対象音、Velocity、長さ、確率を編集できない。
- 手段: 16行×ステップのGridを追加し、追加/削除、Velocity、Nudge、Repeat/Ratchet、Probability、選択範囲、コピーを実装する。
- UI/UX: `Sequence`モードでのみ全幅表示する。初期表示は16分音符とパッド行だけにし、Velocity/Chanceレーンは選択時に出す。
- 完了判定: マウスとパッドの双方でステップ追加、削除、Velocity変更ができ、再生中に編集してもタイミングが乱れない。

#### P1-3 非破壊Quantize、Swing、Nudge、Humanize

- 状態: 完了。元打点と再生打点をProject内で分離し、`Tight / Natural / Loose`、Grid、Strength 0～100%、Swing 50～75%、全体Nudge ±50 ms、再現可能なHumanize 0～20 ms、完全Reset、Undo/Redoを追加した。Sequenceで選択したイベントは±5 ms単位で個別Nudgeできる。
- ユーザー成果: 走り/もたりを直しつつ、自分のグルーヴを残せる。
- 現在の摩擦: QuantizeはRepeat Rateのグリッドへ一律吸着し、適用量、Swing、個別Nudge、元タイミング復元がない。
- 手段: GridとStrength 0～100%、Swing、選択イベントだけのNudge、Humanize範囲を追加し、元イベント時刻を保持する。
- UI/UX: 通常画面の`Q 1/16`は`Feel`メニューへ統合し、`Tight / Natural / Loose`を入口にする。詳細数値は展開時だけ表示する。
- 完了判定: 50% Quantizeで元のズレを半分残し、Resetで完全に元へ戻せる。Swing適用で偶数ステップだけが意図どおり移動する。

#### P1-4 Pattern/Sceneと演奏中の切替

- 状態: 完了。8 Pattern slots、空きslotへのDuplicate、1→2→4小節Double、個別Clear、`Next beat / Next bar / Pattern end` Launch Quantize、予約先表示を追加した。ScenesでPatternを最大64個のSong orderへ並べ、3つ以上を再生停止なしでPattern末尾ごとに順送りできる。Pattern、Scene順序、Probability/Ratchetを含む編集内容はProjectへAutosaveされる。
- ユーザー成果: 1つのループからAメロ、フィル、サビを作り、ライブで切り替えられる。
- 現在の摩擦: ループは1本のみで、Variationの複製、次小節での切替、曲順がない。
- 手段: 8～16 Pattern slots、Duplicate、Double、Clear、Scene順序、1拍/1小節/パターン末尾のLaunch Quantizeを追加する。
- UI/UX: Performでは4～8個のSceneだけを大きく表示し、編集機能はSequenceへ置く。次に鳴るSceneは点滅、現在のSceneは固定色で区別する。
- 完了判定: 再生を止めずにPattern複製、編集、次小節切替ができ、3つ以上を並べて曲として再生できる。

#### P1-5 Pad Mixerと一括選択

- 状態: 完了。PadごとのVolume、Pan、Tune、Muteと一時的なSoloを追加した。Shift+Padで複数選択し、Mixerモーダルから一括編集、Reset、20段階Project Undo、原音とのA/B比較ができる。設定はKit/Projectへ保存され、再生とWAV書き出しの両方へ反映される。SoloとA/Bは別Kit・別Projectへ持ち越さない。
- ユーザー成果: スネアだけ少し下げる、ハットを左右へ振る、キックだけ確認する、といった判断を素早く行える。
- 現在の摩擦: Master VolumeとSensitivityはあるが、Padごとの音量、Pan、Tune、Mute/Soloがない。
- 手段: PadごとにVolume、Pan、Tune、Mute、Soloを追加する。複数Pad選択で同じ変更を一括適用する。
- UI/UX: 選択PadのInspectorに3つの主要ノブだけ表示し、Mute/SoloはPad上のホバーまたはMixerモードで出す。常時16フェーダーは置かない。
- 完了判定: 再生中に任意PadをSoloし、音量・Pan・Tuneを変更して、UndoとA/B比較ができる。

#### P1-6 Auto ChopとPad展開

- 状態: 完了。Waveform EditorからTransient、Equal、Manual、Live/Lazy Chopを開き、2/4/8/16 Sliceまたは波形クリック／演奏中のPad tapでマーカーを作れる。実行前に必要Slice数と利用可能Pad数を比較し、不足時は書き込まない。Keep Original、Play Through、Slice共通Chokeを選び、一操作で連続Padへ展開してProjectへ保存し、Undo一回で割当を戻せる。8秒4小節相当の16 Slice実測は0.066秒。
- ユーザー成果: ドラムブレイクや録音したフレーズを、すぐ16パッドで組み替えられる。
- 現在の摩擦: 1録音=1Padで、長い素材を分割する導線がない。
- 手段: Transient、Equal、Manual、Live/Lazy Chopを実装し、空きPadへ順番に割り当てる。Keep Original、Play Through、Choke Groupを選べるようにする。
- UI/UX: 波形へマーカーを直接置き、パッドを叩いて各Sliceを試聴する。空きPad不足は実行前に視覚表示する。
- 完了判定: 4小節のドラムループを30秒以内に8～16 Sliceへ分割し、その場で並べ替えて録音できる。

#### P1-7 Tempo DetectとTime Stretch

- 状態: 完了。Sample EditorにTempo Detect、Half/Double補正、`Off / Repitch / Stretch`を追加した。Onset autocorrelationと小節長候補から40～240 BPMおよび1/2/4/8/16小節を推定し、通常域を優先する。Pitch-independent Stretchはaudiotsm WSOLAを使用し、Project BPM変更時にキャッシュを再生成する。出力長は推定小節末へ厳密に揃え、ライブ再生とMaster/Stem/Bundleへ反映する。既知4小節ループ90/120/140 BPMは誤差1 BPM以内、1.25倍Stretchの440 Hzは439.94 Hzだった。
- ユーザー成果: 取り込んだループを曲BPMへ合わせ、Pitchは変えずに使える。
- 現在の摩擦: サンプルは固定速度で再生され、BPM変更に追従しない。
- 手段: BPM/小節数の推定、Half/Double補正、RepitchとPitch-independent Stretchを追加する。ドラム用Transientモードを優先する。
- UI/UX: 推定値は`Fits 120 BPM`のような結果で示し、BPM数値の編集はSample詳細へ置く。処理前後を同期試聴できるようにする。
- 完了判定: 90～140 BPMの既知ループでHalf/Doubleを含め正しい拍長を選べ、曲テンポ変更時も小節末がずれない。

#### P1-8 生ドラムらしい連打とArticulation

- 状態: 完了。Ghost/Soft/Mid/Hard/Accentの5ゾーンを隣接層クロスフェードで接続し、Velocity 70を弱打、100を強打として維持した。同一ゾーン内は直前サンプルを除外し、表示しない±1.5% Gain variationを加える。打撃時のPitch再サンプルは実機p99を悪化させたため外し、固定Tuneは起動時、Mixer Tuneは編集時に事前生成する。GM CC4 Hat PedalはClosed/Semi/Openを連続選択し、4 Hat音色では対応するOpen音色へ移行して共通Chokeを維持する。Exportではvariationをイベント位置から再現可能にする。
- ユーザー成果: 同じPadを連打しても機械的な「同じ音の繰り返し」に聞こえず、強弱で音色が自然に変わる。
- 現在の摩擦: Soft/Mid/Hardの3帯とランダム選択はあるが、直前と同じサンプルの回避、帯域境界の連続性、Pad別レイヤー設計、詳細なArticulationがない。
- 手段: Round-robin履歴、同一サンプル連続回避、5～8段階のVelocity zone、隣接層クロスフェード、微小なGain/Pitch variationを導入する。HatはClosed/Semi/Openを同一グループとして制御可能にする。
- UI/UX: 通常は`Natural`プリセットだけ見せ、Settings > Sound Engineで`Tight / Natural / Loose`を選ぶ。レイヤー数や乱数値は表に出さない。
- 完了判定: 同一Velocityの16分連打で直前サンプルの反復がなく、Velocity 69/70や99/100で不自然な音量段差が発生しない。

#### P1-9 ハイハット、ライド、スネアを各4音色へ拡張

- 状態: 完了。既存のCC BY 3.0 Salamander Drumkit素材から、Snare `Tight / Warm / Deep / Bright`、Hat `Tight / Dry / Bright / Dark`、Ride `Dry / Bright / Dark / Washy`を追加した。全音色がSoft/Mid/Hardと複数Round-robin候補を持ち、Hatは4組のClosed/Openが同じChoke groupを維持する。選択Padの左右Sound操作は主要楽器内の4音色だけを循環し、Kit/Project保存とMIDI/WAV/Stem exportへ反映される。Velocity 100の実素材RMS差は各楽器内4～16%に補正した。
- ユーザー成果: 曲調や演奏スタイルに合う主要音を、外部音源を探さず選べる。
- 現在の摩擦: ハイハット、ライド、スネアの音色選択肢が少なく、Kitを切り替えても曲の印象を大きく変えにくい。
- 手段: ハイハット、ライド、スネアを各4音色、合計12音色へ拡張する。候補は、ハイハットを`Tight / Dry / Bright / Dark`、ライドを`Dry / Bright / Dark / Washy`、スネアを`Tight / Warm / Deep / Bright`とし、実際の素材を試聴して最終名称を決める。
- 音源要件: 各音色にSoft/Mid/Hard以上のVelocity layerとRound-robin候補を用意する。ハイハットはClosed/Openを同じ音色セットで対にし、既存のチョーク動作を維持する。音色間の知覚音量を揃え、切替時の急な音量差を防ぐ。
- UI/UX: 選択PadのSound操作から前後試聴でき、ループ再生中にも文脈内で比較できる。通常画面には音源ファイル名やレイヤー数を出さず、短い音色名だけ表示する。
- 完了判定: 3楽器すべてで4音色を選択・保存でき、各音色が弱打から強打まで変化し、同一Velocityの高速連打で同じサンプルが不自然に反復しない。ハイハットは全4音色でClosedがOpenを正しく止める。

#### P1-10 Sample Browserの試聴と整理

- 状態: 完了。PerformのBrowseから右ドロワーを開き、文字検索、Type、Built-in/User、Kit A-D、Favorite、Recentで内蔵音色とUser Sampleを絞り込める。候補行の1クリック目は現在PadのVolume/Pan/Tuneを保った文脈内PreviewでProjectを変更せず、`Use Sound`の2クリック目で割り当ててAutosaveする。置換はProject Undoで戻せる。欠落Sampleは赤いMissing表示とRelinkを出し、指定フォルダ以下から参照中の同名ファイルを一度に探索・復旧する。ドロワー表示中も左側の演奏Padを維持する。
- ユーザー成果: 曲を止めずに、合う音を短時間で見つけられる。
- 現在の摩擦: 内蔵音は前後送り、外部音はファイル選択/ドロップ中心で、検索、Favorite、Recent、文脈内試聴がない。
- 手段: Type、Kit、Favorite、Recentの軽量ブラウザ、選択時Preview、ループ再生中のIn-context audition、欠落ファイルRelinkを追加する。
- UI/UX: ブラウザは必要時だけ右から開き、Padは見えるままにする。音名やパスの一覧を常時表示しない。
- 完了判定: Kick候補を再生中に連続試聴し、2クリック以内に置換、Undoで戻せる。欠落サンプルをフォルダ指定一回でまとめて再リンクできる。

### P2: 「仕上げる、外へ渡す、広げる」

#### P2-1 Perform FXとResample/Bounce

- 状態: 完了。演奏面を覆わない`FX`モーダルへFilter、Delay、Stutter、Crushの4 Macroを追加し、数値を出さないドラッグ式Stripと微調整ボタン、A/B、Resetを提供する。Record/Overdub中のStrip操作は拍位置付きAutomationとしてPattern、Project、Loop Undoへ保存され、Loop再生とWAV/Bounceで再現される。`Bounce Loop`は現在のPad Mixer、FX、Automation、Master Limiter込みの2小節以上のLoopをバックグラウンド生成し、再生状態と位相を変えず次の空きPadへCustom Sampleとして割り当てる。割当はProject Undoで戻せる。実素材48 kHz renderと2小節非停止Bounceの自動試験を追加した。
- ユーザー成果: 演奏の動き込みで音を変え、その結果を新しいPadへ固定できる。
- 現在の摩擦: サンプル入力は外部音中心で、アプリ出力、ループ、FX操作をPadへ録り戻せない。
- 手段: Filter、Delay、Reverb、Stutter、Crusherなど少数のPerform FXと、Master/選択Pad/Loopを空きPadへBounceする機能を追加する。
- UI/UX: Perform FXは16Padを奪わず、画面上の4つのXY/Stripまたは修飾キー付きPadモードで扱う。録り戻しはドラッグ操作を中心にする。
- 完了判定: 再生中の2小節とFX操作を新規Padへ録り、元ループを止めずに差し替えられる。

#### P2-2 Effects、Bus、Master保護

- 状態: 完了。Mixerを`Mix / FX`タブ化し、単一または複数Padへ`Punch / Air / Space`の固定チェーンMacroと4 Bus割り当てを追加した。値は`Off / Low / Med / High`で示し、詳細パラメータを演奏面から隠した。ライブ音とWAV Exportへ同じ音作りを適用し、A/B、Reset、統一Undo、Kit保存へ統合した。Master Exportは0.98 ceiling limiterで保護し、ライブは重なりが閾値を超えた時だけヘッダーへ`PEAK`を1.2秒表示する。16 Pad同時最大打撃を含む自動試験でクリップ防止を確認した。
- ユーザー成果: 外部DAWなしでも耳に痛くない、音量の揃ったデモを作れる。
- 現在の摩擦: Padごとの音量/音作り、Send、Master peak保護がない。
- 手段: Padまたは4 BusにEQ/Filter/Compressor/Saturation、Send Reverb/Delay、Master Limiterを追加する。まず固定チェーンと少数Macroで提供する。
- UI/UX: `Punch / Air / Space`など結果ベースのMacroを先に見せ、周波数やRatioはAdvancedへ置く。Peak時だけヘッダーに警告する。
- 完了判定: Masterのクリップを防ぎ、Kit変更後もPad間の知覚音量差を短時間で補正できる。

#### P2-3 MIDI Clock/Transport同期

- 状態: 完了。WinMM realtime messageでMIDI Clock In/Out 24 PPQN、Start、Continue、Stopを高優先度Audio threadへ統合した。Clock sourceは`Auto / Internal / External`で、Autoは受信時Externalへ切り替わり、1秒途絶でInternalへ復帰する。External中はBPM操作をLock表示し、96 tick中央値でTempoを安定化する。Settings > MIDI SyncでOutput port、Clock Out、±100 ms offsetを設定できる。120 BPM・10分・28,800 tickシミュレーションで累積位相ずれは1/16未満、実機WinMM列挙でSTARRYPAD MINIの入出力各3 portを確認した。
- ユーザー成果: DAWや別機材と同じテンポ・小節頭で演奏、録音できる。
- 現在の摩擦: 内部BPMのみで、外部Start/Stop/Clockに追従しない。
- 手段: MIDI Clock In/Out、Start/Stop/Continue、遅延補正、Clock source Auto/Internal/Externalを実装する。将来のAbleton Linkは別途評価する。
- UI/UX: 外部同期中だけBPM欄をLock表示にする。詳細ポートと補正値はSettingsへ置く。
- 完了判定: 10分再生で1/16音符以上の累積ずれがなく、外部Startで同じ小節頭から開始する。

#### P2-4 Stem/Pad別ExportとProject Bundle

- 状態: 完了。ShareからMaster WAV、MIDI、16 Pad Stem、Project Bundleを選べる。StemはMasterと同じBPM、開始位置、長さ、48 kHz stereoで書き出し、MIDIとtempo metadataを同梱する。BundleはProject、Master、全Stem、MIDI、使用中のCustom Sampleを1つのZIPへまとめる。Pad別レンダーの合計がMasterと一致する自動試験を追加した。
- ユーザー成果: DAWで続きを作る人へ、ミックスし直せる材料を一度で渡せる。
- 現在の摩擦: Master WAVとMIDIだけで、Pad別音声、Scene範囲、サンプル同梱がない。
- 手段: Master、各Pad、各Bus、Loop regionのWAV exportと、MIDI、Project、使用サンプルをまとめたBundle exportを追加する。
- UI/UX: 常時表示中のExport WAV/MIDIはProjectメニューへ移し、普段は`Share`一つから用途を選ぶ。
- 完了判定: DAWへ全Pad stemを読み込むと同じBPM・同じ長さ・同じ開始位置で重なり、合計がMasterと一致する。

#### P2-5 UIスケーリング、キーボード、アクセシビリティ

- 状態: 完了。1040×820の安定した論理キャンバスを100/125/150%および任意リサイズ可能なウィンドウへaspect-fitし、余白を含む物理座標を論理座標へ逆変換して表示とHit領域を一致させた。SettingsのDisplay sizeと`Ctrl+0/+/-`で倍率変更できる。`Ctrl+Z/Y/S/O/N`、Tab/Shift+Tabの表示中Control順フォーカス、Enter実行、矢印のPad選択、Enter試聴を追加した。フォーカスは太枠、Mute/Soloは文字、接続は文言を併記し、色だけに依存しない。記号ボタンは停止Hover時だけTooltipを出す。通常SettingsからDiagnostics数値を外した。100/150%と800×600 render、中央座標逆変換、自動試験で確認した。
- ユーザー成果: 異なる画面サイズ、マウス、キーボードでも迷わず操作でき、状態を色だけに頼らず認識できる。
- 現在の摩擦: 固定1024x820、文字ボタン中心、色によるPad区別、Tooltipなし、Undo/Redoなどの標準ショートカット不足。
- 手段: リサイズ可能レイアウト、100/125/150% UI scale、フォーカス順、標準ショートカット、Tooltip、色+形/アイコンの状態表現を追加する。
- UI/UX: Transportは一般的なRecord/Play/Stop/Undoアイコンを使い、未知のアイコンだけTooltipを付ける。Pad名は常に読めるコントラストを保つ。
- 完了判定: 1280x720、1920x1080、Windows 150%で欠け/重なりなし。キーボードだけで主要操作へ到達できる。

## 5. 画面構成の推奨

### Perform

- 現在の16Padを主役として維持する。
- 常時表示は接続状態、Kit、Master Volume、Transport、現在Patternだけに絞る。
- 選択Padの編集値、診断値、MIDI番号、Export、入力デバイスは常時表示しない。
- Record待機、Recording、Overdub、次に切り替わるPatternは、演奏中でも一目で区別できる。

### Sample

- 16Padを残し、選択Padの波形、録音レベル、Start/End、主要再生モードを表示する。
- Auto Chopや詳細Toolsは波形から開く。
- `Use Kit Sound`はカスタムサンプル使用時だけ出す。

### Sequence

- 下部の一本線タイムラインを16行Gridへ拡張する。
- 初期状態ではNoteだけ、選択後にVelocity/Chance/Nudgeを段階表示する。
- Pattern複製、長さ、Scene切替はこの画面へ集約する。

### Settings / Project

- Settings: MIDI、Audio、Latency、Pad calibration、診断。
- Project: New/Open/Recent/Save As/Collect/Export。
- 技術値を隠すことと、録音状態や未保存状態を隠すことは分ける。前者は低頻度情報、後者は演奏判断に必要な情報である。

## 6. 実装順

1. P0-0 Sample attack QAを実施し、現在の弱打スネア遅延を解消する。
2. P0-4 Project分離とP0-5 Undo基盤。後続編集の安全性を先に作る。
3. P0-1 Calibration、P0-2再接続、P0-3 Audio setup。楽器としての信頼性を固める。
4. P0-6 Count-in、P0-7 Capture、P0-8 Sampling改善。アイデアを失わない流れを完成させる。
5. P1-1 Waveform Editor、P1-5 Pad Mixer、P1-8 Round-robin。音源としての質を上げる。
6. P1-2 Grid、P1-3 Feel、P1-4 Pattern/Scene、P1-9主要3楽器の4音色化。ルーパーと音色選択を制作道具へ進化させる。
7. P1-6 Chop、P1-7 Stretch、P1-10 Browser。サンプラーとしての深さを足す。
8. P2項目を利用実績に応じて追加する。

## 7. 今は足さない方がよいもの

- フルDAW相当の無制限Audio/MIDI trackと巨大なLinear Arranger。
- 数十個の常時表示エフェクトと複雑なルーティング表。
- VSTホスト、AI Stem分離、クラウド同期、巨大な追加音源ストア。

これらは商用ソフトには存在するが、現段階の主要な不満である入力追従、録音失敗、編集不能、1ループから先へ進めない問題を直接解決しない。P0/P1の利用テスト後に必要性を判断する。

## 8. 公式調査資料

- Native Instruments, Maschine 3 software manual: https://docs.native-instruments.com/ni-tech-manuals/maschine-software-manual/en/quick-reference
- Native Instruments, Sampling and sample mapping: https://docs.native-instruments.com/ni-tech-manuals/maschine-mk3-manual/en/sampling-and-sample-mapping
- Akai Professional, MPC 3 FAQ: https://support.akaipro.com/en/support/solutions/articles/69000857771-mpc3-faq
- Akai Professional, MPC Browser: https://support.akaipro.com/en/support/solutions/articles/69000871930-akai-pro-mpc-series-understanding-the-mpc-s-browser
- Akai Professional, Count-in and metronome: https://support.akaipro.com/en/support/solutions/articles/69000857890-akai-pro-mpc-series-editing-the-count-in-and-metronome
- Ableton, Live 12 Simpler and instruments: https://www.ableton.com/en/manual/live-instrument-reference/
- Ableton, Drum Racks: https://www.ableton.com/en/live-manual/12/instrument-drum-and-effect-racks/
- Ableton, MIDI editing: https://www.ableton.com/en/live-manual/12/editing-midi/
- Ableton, Push 3 manual: https://www.ableton.com/en/push/manual/
- Serato, Drum Deck parameter and pad tools: https://support.serato.com/hc/en-us/articles/360001445835-Drum-Deck-Parameter-Pad-Tools-Panel
- Serato, Song View: https://support.serato.com/hc/en-us/articles/360001463116-Song-View-Overview
- Serato, Export: https://support.serato.com/hc/en-us/articles/6239823223183-Misc-Export
- Koala Sampler manual: https://manual.koalasampler.com/
- Koala Sampler, Samurai: https://www.koalasampler.com/samurai/
- Koala Sampler, Mixer: https://www.koalasampler.com/mixer/
- Toontrack, Superior Drummer 3: https://www.toontrack.com/product/superior-drummer-3/
- Toontrack, E-drums setup and response: https://www.toontrack.com/blog/using-e-drums-with-ezdrummer-3-superior-drummer-3/
- XLN Audio, Addictive Drums 2 overview: https://support.xlnaudio.com/hc/en-us/articles/16382433436701-What-is-Addictive-Drums-2
- XLN Audio, Addictive Drums 2 manual: https://assets.xlnaudio.com/documents/addictive-drums-manual.pdf
