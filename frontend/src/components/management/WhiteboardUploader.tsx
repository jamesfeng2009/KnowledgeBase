/**
 * 白板拍照上传 + VLM 解析组件
 *
 * 交互流程（状态机）：
 *   idle       → 显示拖拽上传区 + "选择白板照片"按钮，支持 image/*
 *   uploading  → 显示上传进度条 + "正在上传白板照片..."
 *   parsing    → 显示 AI 解析动画（旋转图标）+ "AI 正在理解白板内容..."
 *   done       → 展示生成的会议纪要预览（摘要 / 关键要点 / 行动项）
 *                提供"重新上传"与"使用此纪要"按钮
 *   error      → 显示错误信息 + "重试"按钮
 *
 * 上传与解析在后端为单次接口调用（uploadWhiteboard），
 * 前端通过两段式状态动画为用户提供更清晰的视觉反馈：
 *   1. 立即发起 API 请求，并进入 uploading 状态显示进度条
 *   2. 至少展示 800ms 上传动画后切换到 parsing 状态
 *   3. API 返回后切换到 done / error 状态
 *
 * 集成方式：
 *   - 作为 React Island 以 client:only="react" 挂载（依赖浏览器 FormData / localStorage）
 *   - 由于 client:only props 不可序列化函数，故通过 window CustomEvent
 *     'ekb:whiteboard-minutes' 向宿主页面回传生成的会议纪要
 *   - 同时保留 onMinutesGenerated 回调，便于在纯 React 上下文中复用
 *
 * 对应后端 3.18 多模态知识处理（白板拍照入库）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { uploadWhiteboard } from '@/lib/apis/multimodal';
import type { WhiteboardMinutes } from '@/lib/apis/multimodal';
import styles from './multimodal.module.css';

interface WhiteboardUploaderProps {
  /** 纪要生成后的回调（在纯 React 上下文中使用时有效） */
  onMinutesGenerated?: (minutes: WhiteboardMinutes) => void;
}

/** 组件状态机 */
type UploadStatus = 'idle' | 'uploading' | 'parsing' | 'done' | 'error';

/** 上传阶段最短展示时长（ms），保证用户能看到上传动画 */
const MIN_UPLOAD_PHASE_MS = 800;

/** 接受的文件类型 */
const ACCEPT = 'image/*';

/** 校验文件类型是否为图片 */
function isImageFile(file: File): boolean {
  return file.type.startsWith('image/');
}

/** 全局事件名：通知宿主页面已生成会议纪要 */
const MINUTES_EVENT = 'ekb:whiteboard-minutes';

export function WhiteboardUploader({ onMinutesGenerated }: WhiteboardUploaderProps) {
  const [status, setStatus] = useState<UploadStatus>('idle');
  const [minutes, setMinutes] = useState<WhiteboardMinutes | null>(null);
  const [errorMsg, setErrorMsg] = useState<string>('');
  const [dragover, setDragover] = useState(false);
  const [selectedFileName, setSelectedFileName] = useState<string>('');

  const fileInputRef = useRef<HTMLInputElement>(null);
  // 保留最新选择的文件，用于失败后重试
  const lastFileRef = useRef<File | null>(null);
  // 防止重复并发处理
  const processingRef = useRef(false);

  // 组件卸载时重置处理标记，避免内存泄漏引用
  useEffect(() => {
    return () => {
      processingRef.current = false;
    };
  }, []);

  /** 核心处理逻辑：上传 → 解析 → 完成 / 失败 */
  const processFile = useCallback(async (file: File) => {
    if (processingRef.current) return;
    if (!isImageFile(file)) {
      alert('仅支持图片格式（PNG / JPG / JPEG 等）');
      return;
    }
    processingRef.current = true;
    lastFileRef.current = file;
    setSelectedFileName(file.name);
    setErrorMsg('');
    setMinutes(null);

    // 1. 进入 uploading 状态，立即发起 API 请求
    setStatus('uploading');
    const apiPromise = uploadWhiteboard(file);

    // 2. 至少展示 MIN_UPLOAD_PHASE_MS 的上传动画，再切换到 parsing 状态
    await new Promise((resolve) => setTimeout(resolve, MIN_UPLOAD_PHASE_MS));
    if (!processingRef.current) return; // 组件已卸载
    setStatus('parsing');

    // 3. 等待 API 返回
    try {
      const result = await apiPromise;
      if (!processingRef.current) return;
      setMinutes(result);
      setStatus('done');
    } catch (err) {
      if (!processingRef.current) return;
      setErrorMsg(err instanceof Error ? err.message : '白板解析失败，请重试');
      setStatus('error');
    } finally {
      processingRef.current = false;
    }
  }, []);

  /** 选择文件（点击按钮 / 点击上传区） */
  const handleSelectClick = () => {
    if (status === 'uploading' || status === 'parsing') return;
    fileInputRef.current?.click();
  };

  /** 文件选择回调 */
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) void processFile(file);
    // 重置 input 的 value 以便重复选择同一文件
    e.target.value = '';
  };

  /** 拖拽相关事件 */
  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    if (status === 'uploading' || status === 'parsing') return;
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
    if (status === 'uploading' || status === 'parsing') return;
    const file = e.dataTransfer.files?.[0];
    if (file) void processFile(file);
  };

  /** 重新上传：回到 idle 状态 */
  const handleReset = () => {
    setStatus('idle');
    setMinutes(null);
    setErrorMsg('');
    setSelectedFileName('');
    lastFileRef.current = null;
  };

  /** 重试：使用上次选择的文件重新处理 */
  const handleRetry = () => {
    const file = lastFileRef.current;
    if (file) {
      void processFile(file);
    } else {
      // 没有上次文件记录，回退到 idle
      handleReset();
    }
  };

  /** 使用此纪要：触发回调 + 全局事件 */
  const handleUseMinutes = () => {
    if (!minutes) return;
    // 优先调用 props 回调（纯 React 上下文）
    onMinutesGenerated?.(minutes);
    // 同时派发全局事件（Astro Island 集成场景）
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent<WhiteboardMinutes>(MINUTES_EVENT, { detail: minutes })
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
          aria-label="选择白板照片上传"
        >
          <div className={styles.uploadZoneIcon} aria-hidden="true">📸</div>
          <div className={styles.uploadZoneTitle}>
            拖拽白板照片到此处，或
            <span className={styles.uploadZoneTitleHint}> 点击选择文件</span>
          </div>
          <div className={styles.uploadZoneDesc}>
            支持 PNG / JPG / JPEG / WEBP 等图片格式，AI 将自动理解白板内容并生成会议纪要
          </div>
          <button
            type="button"
            className={`btn btn-primary btn-sm ${styles.uploadZoneBtn}`}
            onClick={(e) => {
              e.stopPropagation();
              handleSelectClick();
            }}
          >
            选择白板照片
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

  /** uploading：上传进度条 */
  if (status === 'uploading') {
    return (
      <div className={styles.wrapper}>
        <div className={styles.statusCard}>
          <div className={styles.statusIcon} aria-hidden="true">📤</div>
          <div className={styles.statusText}>正在上传白板照片...</div>
          {selectedFileName && (
            <div className={styles.statusHint}>{selectedFileName}</div>
          )}
          <div className={styles.progressWrap}>
            <div className={styles.progressBar}>
              <div className={styles.progressFill} />
            </div>
            <div className={styles.progressPercent}>上传中，请稍候</div>
          </div>
        </div>
      </div>
    );
  }

  /** parsing：AI 解析动画 */
  if (status === 'parsing') {
    return (
      <div className={styles.wrapper}>
        <div className={styles.statusCard}>
          <div className={styles.pulseIcon} aria-hidden="true">🤖</div>
          <div className={styles.statusText}>AI 正在理解白板内容...</div>
          <div className={styles.statusHint}>
            正在识别文字、图表与结构，生成会议纪要
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
            白板解析失败
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

  /** done：展示生成的会议纪要预览 */
  if (status === 'done' && minutes) {
    return (
      <div className={styles.wrapper}>
        <div className={styles.resultCard}>
          <div className={styles.resultHeader}>
            <span className={styles.resultTitle}>
              <span aria-hidden="true">📋</span>
              <span>会议纪要预览</span>
            </span>
            <span className={styles.resultBadge}>AI 生成</span>
          </div>

          {/* 摘要 */}
          <div className={styles.summaryBlock}>
            <div className={styles.blockLabel}>
              <span aria-hidden="true">📝</span>
              <span>摘要</span>
            </div>
            <div className={styles.summaryText}>
              {minutes.summary || '（未生成摘要）'}
            </div>
          </div>

          {/* 关键要点 */}
          <div>
            <div className={styles.blockLabel}>
              <span aria-hidden="true">🔑</span>
              <span>关键要点</span>
            </div>
            {minutes.key_points && minutes.key_points.length > 0 ? (
              <ul className={styles.keyPointsList}>
                {minutes.key_points.map((point, idx) => (
                  <li key={idx} className={styles.keyPointItem}>
                    {point}
                  </li>
                ))}
              </ul>
            ) : (
              <div className={styles.actionEmpty}>未识别到关键要点</div>
            )}
          </div>

          {/* 行动项 */}
          <div>
            <div className={styles.blockLabel}>
              <span aria-hidden="true">✅</span>
              <span>行动项</span>
            </div>
            {minutes.action_items && minutes.action_items.length > 0 ? (
              <div className={styles.actionList}>
                {minutes.action_items.map((item, idx) => (
                  <div key={idx} className={styles.actionItem}>
                    <div className={styles.actionContent}>{item.content}</div>
                    <div className={styles.actionMeta}>
                      <span className={styles.actionMetaItem}>
                        <span aria-hidden="true">👤</span>
                        <span>负责人：</span>
                        <span className={styles.actionMetaItemStrong}>
                          {item.assignee || '未指定'}
                        </span>
                      </span>
                      <span className={styles.actionMetaItem}>
                        <span aria-hidden="true">📅</span>
                        <span>截止：</span>
                        <span className={styles.actionMetaItemStrong}>
                          {item.deadline || '未指定'}
                        </span>
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className={styles.actionEmpty}>未识别到行动项</div>
            )}
          </div>

          {/* 操作按钮 */}
          <div className={styles.actions}>
            <button type="button" className="btn btn-ghost btn-sm" onClick={handleReset}>
              重新上传
            </button>
            <button type="button" className="btn btn-primary btn-sm" onClick={handleUseMinutes}>
              使用此纪要
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 兜底：理论上不会到达，返回 null 保证类型完整
  return null;
}

export default WhiteboardUploader;
