/**
 * 扫描件 OCR 处理组件
 *
 * 交互流程（状态机）：
 *   idle        → 显示拖拽上传区 + "选择扫描件"按钮，支持 application/pdf
 *   processing  → 显示 OCR 进度动画 + "正在进行 OCR 识别..."
 *   done        → 展示识别的文本内容（可编辑 textarea）
 *                  显示识别页数 + 置信度
 *                  提供"复制文本"与"使用此文本"按钮
 *   error       → 显示错误信息 + "重试"按钮
 *
 * OCR 在后端为单次接口调用（ocrScannedPdf），
 * processing 状态使用脉冲图标 + 旋转 spinner 提供视觉反馈，
 * API 返回后切换到 done / error 状态。
 *
 * 集成方式：
 *   - 作为 React Island 以 client:only="react" 挂载（依赖浏览器 FormData / localStorage）
 *   - 由于 client:only props 不可序列化函数，故通过 window CustomEvent
 *     'ekb:ocr-text' 向宿主页面回传识别后的文本
 *   - 同时保留 onOcrComplete 回调，便于在纯 React 上下文中复用
 *
 * 对应后端 3.18 多模态知识处理（扫描件 OCR）。
 *
 * NOTE: 当前 dead code。未在任何 .astro 页面挂载（manage/upload.astro 仅实现
 * 普通文档上传，未集成扫描件 OCR 入口）。保留供 P5 在 manage/upload.astro
 * 或新建 manage/scanned.astro 时挂载使用。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ocrScannedPdf } from '@/lib/apis/multimodal';
import type { OcrResult } from '@/lib/apis/multimodal';
import styles from './multimodal.module.css';

interface ScanProcessorProps {
  /** OCR 完成后的回调（在纯 React 上下文中使用时有效） */
  onOcrComplete?: (text: string) => void;
}

/** 组件状态机 */
type ScanStatus = 'idle' | 'processing' | 'done' | 'error';

/** 接受的文件类型 */
const ACCEPT = 'application/pdf';

/** 全局事件名：通知宿主页面已生成 OCR 文本 */
const OCR_EVENT = 'ekb:ocr-text';

/** 校验文件类型是否为 PDF */
function isPdfFile(file: File): boolean {
  // 部分系统对 PDF 的 MIME 类型识别不一致，同时校验扩展名
  return file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
}

/** 格式化置信度为百分比展示 */
function formatConfidence(confidence: number): string {
  if (!Number.isFinite(confidence)) return '--';
  // 后端可能返回 0~1 或 0~100，统一转换为百分比
  const percent = confidence > 1 ? confidence : confidence * 100;
  return `${percent.toFixed(1)}%`;
}

export function ScanProcessor({ onOcrComplete }: ScanProcessorProps) {
  const [status, setStatus] = useState<ScanStatus>('idle');
  const [ocrResult, setOcrResult] = useState<OcrResult | null>(null);
  const [editedText, setEditedText] = useState<string>('');
  const [errorMsg, setErrorMsg] = useState<string>('');
  const [dragover, setDragover] = useState(false);
  const [selectedFileName, setSelectedFileName] = useState<string>('');
  const [copyHint, setCopyHint] = useState<string>('');

  const fileInputRef = useRef<HTMLInputElement>(null);
  // 保留最新选择的文件，用于失败后重试
  const lastFileRef = useRef<File | null>(null);
  // 防止重复并发处理
  const processingRef = useRef(false);
  // 复制提示定时器引用
  const copyHintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 组件卸载时清理：重置处理标记 + 清除定时器
  useEffect(() => {
    return () => {
      processingRef.current = false;
      if (copyHintTimerRef.current) {
        clearTimeout(copyHintTimerRef.current);
      }
    };
  }, []);

  /** 核心处理逻辑：上传 → OCR 识别 → 完成 / 失败 */
  const processFile = useCallback(async (file: File) => {
    if (processingRef.current) return;
    if (!isPdfFile(file)) {
      alert('仅支持 PDF 格式的扫描件');
      return;
    }
    processingRef.current = true;
    lastFileRef.current = file;
    setSelectedFileName(file.name);
    setErrorMsg('');
    setOcrResult(null);
    setEditedText('');
    setStatus('processing');

    try {
      const result = await ocrScannedPdf(file);
      if (!processingRef.current) return;
      setOcrResult(result);
      setEditedText(result.text || '');
      setStatus('done');
    } catch (err) {
      if (!processingRef.current) return;
      setErrorMsg(err instanceof Error ? err.message : 'OCR 识别失败，请重试');
      setStatus('error');
    } finally {
      processingRef.current = false;
    }
  }, []);

  /** 选择文件（点击按钮 / 点击上传区） */
  const handleSelectClick = () => {
    if (status === 'processing') return;
    fileInputRef.current?.click();
  };

  /** 文件选择回调 */
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) void processFile(file);
    e.target.value = '';
  };

  /** 拖拽相关事件 */
  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    if (status === 'processing') return;
    e.preventDefault();
    e.stopPropagation();
    setDragover(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragover(false);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragover(false);
    if (status === 'processing') return;
    const file = e.dataTransfer.files?.[0];
    if (file) void processFile(file);
  };

  /** 重新上传：回到 idle 状态 */
  const handleReset = () => {
    setStatus('idle');
    setOcrResult(null);
    setEditedText('');
    setErrorMsg('');
    setSelectedFileName('');
    setCopyHint('');
    lastFileRef.current = null;
  };

  /** 重试：使用上次选择的文件重新处理 */
  const handleRetry = () => {
    const file = lastFileRef.current;
    if (file) {
      void processFile(file);
    } else {
      handleReset();
    }
  };

  /** 复制文本到剪贴板 */
  const handleCopy = async () => {
    if (!editedText) {
      alert('暂无可复制的文本');
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        // 优先使用现代 Clipboard API（需 HTTPS 安全上下文）
        await navigator.clipboard.writeText(editedText);
      } else {
        // 兜底方案：临时 textarea + execCommand（非安全上下文 / 旧浏览器）
        // execCommand 已废弃但仍可用，此处通过类型断言避免触发废弃提示
        const textarea = document.createElement('textarea');
        textarea.value = editedText;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        const execCommand = (document as unknown as {
          execCommand: (cmd: string) => boolean;
        }).execCommand;
        execCommand('copy');
        document.body.removeChild(textarea);
      }
      setCopyHint('已复制到剪贴板');
      if (copyHintTimerRef.current) clearTimeout(copyHintTimerRef.current);
      copyHintTimerRef.current = setTimeout(() => setCopyHint(''), 2000);
    } catch {
      alert('复制失败，请手动选择文本复制');
    }
  };

  /** 使用此文本：触发回调 + 全局事件 */
  const handleUseText = () => {
    if (!editedText) {
      alert('暂无可使用的文本');
      return;
    }
    onOcrComplete?.(editedText);
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent<string>(OCR_EVENT, { detail: editedText })
      );
    }
  };

  // ===== 渲染各状态 =====

  /** idle：拖拽上传区 */
  if (status === 'idle') {
    return (
      <div className={styles.wrapper}>
        <div
          className={`${styles.uploadZone}${dragover ? ' ' + styles.dragover : ''}`}
          onClick={handleSelectClick}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              handleSelectClick();
            }
          }}
          aria-label="选择扫描件上传"
        >
          <div className={styles.uploadZoneIcon} aria-hidden="true">📄</div>
          <div className={styles.uploadZoneTitle}>
            拖拽扫描件到此处，或
            <span className={styles.uploadZoneTitleHint}> 点击选择文件</span>
          </div>
          <div className={styles.uploadZoneDesc}>
            支持 PDF 格式的扫描件，AI 将自动识别文字内容
          </div>
          <button
            type="button"
            className={`btn btn-primary btn-sm ${styles.uploadZoneBtn}`}
            onClick={(e) => {
              e.stopPropagation();
              handleSelectClick();
            }}
          >
            选择扫描件
          </button>
          <input
            ref={fileInputRef}
            type="file"
            className={styles.fileInput}
            accept={ACCEPT}
            onChange={handleFileChange}
            aria-hidden="true"
            tabIndex={-1}
          />
        </div>
      </div>
    );
  }

  /** processing：OCR 进度动画 */
  if (status === 'processing') {
    return (
      <div className={styles.wrapper}>
        <div className={styles.statusCard}>
          <div className={styles.pulseIcon} aria-hidden="true">🔍</div>
          <div className={styles.statusText}>正在进行 OCR 识别...</div>
          {selectedFileName && (
            <div className={styles.statusHint}>{selectedFileName}</div>
          )}
          <div className={styles.statusHint}>
            正在逐页识别文字，可能需要一些时间
          </div>
          <span className={styles.spinner} aria-hidden="true" />
        </div>
      </div>
    );
  }

  /** error：错误信息 + 重试 */
  if (status === 'error') {
    return (
      <div className={styles.wrapper}>
        <div className={`${styles.statusCard} ${styles.statusCardError}`}>
          <div className={`${styles.statusIcon} ${styles.statusIconError}`} aria-hidden="true">
            ⚠️
          </div>
          <div className={`${styles.statusText} ${styles.statusTextError}`}>
            OCR 识别失败
          </div>
          <div className={styles.errorText}>
            {errorMsg || '发生未知错误，请重试'}
          </div>
          <div className={styles.actions}>
            <button type="button" className="btn btn-ghost btn-sm" onClick={handleReset}>
              重新选择
            </button>
            <button type="button" className="btn btn-primary btn-sm" onClick={handleRetry}>
              重试
            </button>
          </div>
        </div>
      </div>
    );
  }

  /** done：展示 OCR 识别结果（可编辑） */
  if (status === 'done' && ocrResult) {
    return (
      <div className={styles.wrapper}>
        <div className={styles.resultCard}>
          <div className={styles.resultHeader}>
            <span className={styles.resultTitle}>
              <span aria-hidden="true">📄</span>
              <span>OCR 识别结果</span>
            </span>
            <span className={styles.resultBadge}>AI 识别</span>
          </div>

          {/* 元信息：识别页数 + 置信度 */}
          <div className={styles.ocrMeta}>
            <span className={styles.ocrMetaItem}>
              <span aria-hidden="true">📑</span>
              <span>识别页数：</span>
              <span className={styles.ocrMetaValue}>{ocrResult.pages}</span>
            </span>
            <span className={styles.ocrMetaItem}>
              <span aria-hidden="true">🎯</span>
              <span>置信度：</span>
              <span className={styles.ocrMetaValue}>
                {formatConfidence(ocrResult.confidence)}
              </span>
            </span>
          </div>

          {/* 可编辑文本区 */}
          <div>
            <div className={styles.blockLabel}>
              <span aria-hidden="true">📝</span>
              <span>识别文本（可编辑）</span>
            </div>
            <textarea
              className={styles.ocrTextarea}
              value={editedText}
              onChange={(e) => setEditedText(e.target.value)}
              placeholder="识别的文本内容将显示在此处，您可以手动修正..."
              aria-label="OCR 识别文本编辑区"
            />
          </div>

          {/* 操作按钮 */}
          <div className={styles.actions}>
            {copyHint && (
              <span className={styles.statusHint} style={{ marginRight: 'auto', color: 'var(--success)' }}>
                {copyHint}
              </span>
            )}
            <button type="button" className="btn btn-ghost btn-sm" onClick={handleReset}>
              重新上传
            </button>
            <button type="button" className="btn btn-outline btn-sm" onClick={handleCopy}>
              复制文本
            </button>
            <button type="button" className="btn btn-primary btn-sm" onClick={handleUseText}>
              使用此文本
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 兜底：理论上不会到达，返回 null 保证类型完整
  return null;
}

export default ScanProcessor;
