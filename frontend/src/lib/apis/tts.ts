/**
 * TTS 语音合成 API 客户端。
 *
 * P1: 为 AI 对话回复提供语音输出。
 */

import { API_BASE } from '../api';

// ------------------------------------------------------------------
// 类型定义
// ------------------------------------------------------------------

export interface VoiceItem {
  voice: string;
  description: string;
}

// ------------------------------------------------------------------
// API 函数
// ------------------------------------------------------------------

/**
 * 合成文本为 MP3 音频。
 *
 * 返回一个 Blob URL，可直接用于 `<audio>` 元素的 src 属性。
 * 调用方负责在不再需要时调用 `URL.revokeObjectURL(url)` 释放内存。
 *
 * @param text 要合成的文本
 * @param voice 语音名称（可选，默认使用后端配置）
 * @param rate 语速（可选，如 "+0%" / "-20%"）
 * @param volume 音量（可选，如 "+0%" / "-10%"）
 * @returns Blob URL（MP3 格式）
 */
export async function synthesizeText(
  text: string,
  voice?: string,
  rate?: string,
  volume?: string,
): Promise<string> {
  const token = localStorage.getItem('ekb_access_token');
  if (!token) {
    throw new Error('未登录，请先登录');
  }

  const response = await fetch(`${API_BASE}/api/v1/tts/synthesize`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ text, voice, rate, volume }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData?.message || `TTS 合成失败 (${response.status})`);
  }

  const blob = await response.blob();
  return URL.createObjectURL(blob);
}

/**
 * 获取可用的 TTS 语音列表。
 */
export async function getVoices(): Promise<VoiceItem[]> {
  const token = localStorage.getItem('ekb_access_token');
  if (!token) {
    throw new Error('未登录，请先登录');
  }

  const response = await fetch(`${API_BASE}/api/v1/tts/voices`, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });

  if (!response.ok) {
    throw new Error(`获取语音列表失败 (${response.status})`);
  }

  const result = await response.json();
  return result.data || [];
}
