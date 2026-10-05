// Storage Browser の日本語の表示文言。日本語の文言は同梱されていないので、ここで差し替える。
// よく使う画面（一覧・アップロード・削除・コピー・フォルダ作成・ダウンロード）の文言を日本語にする。
import type { StorageBrowserDisplayText } from '@aws-amplify/ui-react-storage/browser';
import { folderLabel } from './storage';

type Counts = { COMPLETE?: number; FAILED?: number; OVERWRITE_PREVENTED?: number; CANCELED?: number; TOTAL?: number };

const dateText = (date: Date) =>
  new Intl.DateTimeFormat('ja-JP', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);

const actionView = {
  actionCancelLabel: 'キャンセル',
  actionExitLabel: '戻る',
  actionDestinationLabel: '行き先',
  statusDisplayCanceledLabel: 'キャンセル',
  statusDisplayCompletedLabel: '完了',
  statusDisplayFailedLabel: '失敗',
  statusDisplayInProgressLabel: '実行中',
  statusDisplayTotalLabel: '合計',
  statusDisplayQueuedLabel: '待機中',
  tableColumnStatusHeader: '状態',
  tableColumnFolderHeader: 'フォルダ',
  tableColumnNameHeader: '名前',
  tableColumnTypeHeader: '種類',
  tableColumnSizeHeader: 'サイズ',
  tableColumnProgressHeader: '進み具合',
};

/** 完了のメッセージ。件数を「完了 3件・失敗 1件」の形にまとめる。 */
function completeMessage(counts: Counts | undefined, verb: string) {
  const { COMPLETE = 0, FAILED = 0, OVERWRITE_PREVENTED = 0, CANCELED = 0 } = counts ?? {};
  const parts = [
    COMPLETE && `${verb}しました ${COMPLETE}件`,
    FAILED && `失敗 ${FAILED}件`,
    OVERWRITE_PREVENTED && `上書きしなかった ${OVERWRITE_PREVENTED}件`,
    CANCELED && `キャンセル ${CANCELED}件`,
  ].filter(Boolean);
  const type = FAILED ? 'error' : OVERWRITE_PREVENTED || CANCELED ? 'warning' : 'success';
  return { content: parts.join('・') || `${verb}するものがありません`, type } as const;
}

export const STORAGE_TEXT_JA: StorageBrowserDisplayText = {
  LocationsView: {
    title: 'ファイル置き場',
    searchPlaceholder: 'フォルダを絞り込む',
    searchSubmitLabel: '検索',
    searchClearLabel: '検索をクリア',
    loadingIndicatorLabel: '読み込み中',
    getDateDisplayValue: dateText,
    getListLocationsResultMessage: (data) => {
      if (!data || data.isLoading) return undefined;
      if (data.hasError) return { type: 'error', content: data.message ?? 'フォルダを読み込めませんでした' };
      if (data.items?.length === 0) return { type: 'info', content: '見られるフォルダがありません' };
      return undefined;
    },
    getPermissionName: (permissions) =>
      permissions.includes('write') || permissions.includes('delete') ? '読み書き' : '読み取り',
    getDownloadLabel: (fileName) => `${fileName} をダウンロード`,
    tableColumnBucketHeader: '置き場',
    tableColumnFolderHeader: 'フォルダ',
    tableColumnPermissionsHeader: '権限',
    tableColumnActionsHeader: '操作',
  },
  LocationDetailView: {
    searchPlaceholder: 'このフォルダを検索',
    searchSubmitLabel: '検索',
    searchClearLabel: '検索をクリア',
    searchSubfoldersToggleLabel: 'サブフォルダも含める',
    loadingIndicatorLabel: '読み込み中',
    getDateDisplayValue: dateText,
    tableColumnLastModifiedHeader: '更新日時',
    tableColumnNameHeader: '名前',
    tableColumnSizeHeader: 'サイズ',
    tableColumnTypeHeader: '種類',
    selectFileLabel: 'ファイルを選ぶ',
    selectFolderLabel: 'フォルダを選ぶ',
    selectAllFilesLabel: 'すべて選ぶ',
    getListItemsResultMessage: (data) => {
      if (!data || data.isLoading) return undefined;
      if (data.hasError) return { type: 'error', content: data.message ?? 'ファイルを読み込めませんでした' };
      if (!data.items?.length) return { type: 'info', content: 'ファイルがありません' };
      if (data.hasExhaustedSearch) return { type: 'info', content: '先頭の10,000件までを表示しています' };
      return undefined;
    },
    getActionListItemLabel: (key = '') =>
      ({ Copy: 'コピー', Delete: '削除', 'Create folder': 'フォルダを作る', Upload: 'アップロード', Download: 'ダウンロード' } as Record<string, string>)[key] ?? key,
    // タイトルはバケット名ではなくフォルダの場所を出す
    getTitle: (location) => folderLabel(location.key),
  },
  UploadView: {
    ...actionView,
    title: 'アップロード',
    actionStartLabel: 'アップロード',
    addFilesLabel: 'ファイルを追加',
    addFolderLabel: 'フォルダを追加',
    overwriteToggleLabel: '同じ名前のファイルを上書きする',
    statusDisplayOverwritePreventedLabel: '上書きしなかった',
    getActionCompleteMessage: (data) => completeMessage(data?.counts, 'アップロード'),
    getFilesValidationMessage: (data) => {
      const names = data?.invalidFiles?.map(({ file }) => file.name).join('、');
      return names ? { type: 'warning', content: `160GB を超えるファイルは追加できません: ${names}` } : undefined;
    },
  },
  DeleteView: {
    ...actionView,
    title: '削除',
    actionStartLabel: '削除する',
    confirmationModalTitle: '削除の確認',
    confirmationModalConfirmLabel: '削除する',
    confirmationModalCancelLabel: 'キャンセル',
    confirmationModalFolderListTitle: 'フォルダ:',
    getActionCompleteMessage: (data) => completeMessage(data?.counts, '削除'),
  },
  CopyView: {
    ...actionView,
    title: 'コピー',
    actionStartLabel: 'コピーする',
    searchPlaceholder: 'フォルダを検索',
    searchSubmitLabel: '検索',
    searchClearLabel: '検索をクリア',
    getActionCompleteMessage: (data) => completeMessage(data?.counts, 'コピー'),
  },
  CreateFolderView: {
    ...actionView,
    title: 'フォルダを作る',
    actionStartLabel: '作る',
    folderNameLabel: 'フォルダ名',
    folderNamePlaceholder: '/ は使えません',
    getValidationMessage: () => '/ は使えません',
    getActionCompleteMessage: (data) => {
      const failed = data?.counts?.FAILED;
      return failed ? { type: 'error', content: 'フォルダを作れませんでした' } : { type: 'success', content: 'フォルダを作りました' };
    },
  },
  DownloadView: {
    ...actionView,
    title: 'ダウンロード',
    actionStartLabel: 'ダウンロード',
    getActionCompleteMessage: (data) => completeMessage(data?.counts, 'ダウンロード'),
  },
};
