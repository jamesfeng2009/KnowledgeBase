/* ============================================
   企业知识库 · 高保真原型核心框架
   ============================================ */

// === SVG 图标库 ===
const Icons = {
  home: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>',
  chat: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  search: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  book: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
  doc: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>',
  folder: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  upload: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
  edit: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  users: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  tag: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>',
  chart: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>',
  bell: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
  settings: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  shield: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  graph: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><circle cx="5" cy="5" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><line x1="6.5" y1="6.5" x2="10" y2="10"/><line x1="17.5" y1="6.5" x2="14" y2="10"/><line x1="6.5" y1="17.5" x2="10" y2="14"/><line x1="17.5" y1="17.5" x2="14" y2="14"/></svg>',
  clock: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  message: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
  help: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  plus: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  close: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  check: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  send: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
  paperclip: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
  mic: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>',
  image: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
  at: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-3.92 7.94"/></svg>',
  filter: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>',
  download: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  share: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>',
  bookmark: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>',
  more: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>',
  arrowLeft: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>',
  arrowRight: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>',
  eye: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  trash: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  refresh: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
  copy: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  star: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
  logout: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
  globe: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
  database: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  cpu: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>',
  zap: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  key: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>',
  building: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="2" width="16" height="20" rx="2"/><line x1="9" y1="7" x2="9" y2="7.01"/><line x1="15" y1="7" x2="15" y2="7.01"/><line x1="9" y1="12" x2="9" y2="12.01"/><line x1="15" y1="12" x2="15" y2="12.01"/><line x1="9" y1="17" x2="9" y2="17.01"/><line x1="15" y1="17" x2="15" y2="17.01"/></svg>',
  menu: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>',
  link: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  flag: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>',
  fileText: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/></svg>',
  sparkles: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.8a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3L12 3z"/></svg>',
  layers: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
  grid: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>',
  trending: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>',
  alert: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  video: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>',
  calendar: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
  bolt: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  smartphone: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>',
};

// === Mock 数据 ===
const Mock = {
  currentUser: {
    name: '张明',
    role: '知识管理员',
    avatar: '张',
    dept: '产品中心',
    email: 'zhangming@company.com',
  },
  users: [
    { id: 1, name: '张明', avatar: '张', role: '知识管理员', dept: '产品中心', status: 'active', lastActive: '在线' },
    { id: 2, name: '李华', avatar: '李', role: '编辑者', dept: '研发部', status: 'active', lastActive: '2分钟前' },
    { id: 3, name: '王芳', avatar: '王', role: '查看者', dept: '市场部', status: 'active', lastActive: '1小时前' },
    { id: 4, name: '刘强', avatar: '刘', role: '编辑者', dept: '研发部', status: 'inactive', lastActive: '3天前' },
    { id: 5, name: '陈静', avatar: '陈', role: '管理员', dept: 'IT部', status: 'active', lastActive: '在线' },
    { id: 6, name: '赵伟', avatar: '赵', role: '查看者', dept: '财务部', status: 'active', lastActive: '30分钟前' },
    { id: 7, name: '孙莉', avatar: '孙', role: '编辑者', dept: '人力资源', status: 'active', lastActive: '5分钟前' },
    { id: 8, name: '周杰', avatar: '周', role: '查看者', dept: '运营部', status: 'inactive', lastActive: '1天前' },
  ],
  knowledgeBases: [
    { id: 1, name: '产品研发知识库', icon: '🚀', color: '#4B3FE3', docs: 326, members: 12, updatedAt: '2026-07-04', desc: '产品规划、技术方案、架构设计文档' },
    { id: 2, name: '人力资源制度库', icon: '👥', color: '#00B884', docs: 156, members: 8, updatedAt: '2026-07-03', desc: '员工手册、考勤制度、绩效考核方案' },
    { id: 3, name: '客户成功案例库', icon: '🎯', color: '#FF9500', docs: 89, members: 5, updatedAt: '2026-07-02', desc: '客户案例、解决方案、最佳实践' },
    { id: 4, name: 'IT运维手册', icon: '⚙️', color: '#2196F3', docs: 203, members: 6, updatedAt: '2026-07-01', desc: '系统部署、故障排查、安全规范' },
    { id: 5, name: '财务报销指南', icon: '💰', color: '#FF3B5C', docs: 67, members: 15, updatedAt: '2026-06-30', desc: '报销流程、预算管理、财务制度' },
    { id: 6, name: '市场营销素材库', icon: '📊', color: '#9C27B0', docs: 178, members: 9, updatedAt: '2026-06-28', desc: '品牌资产、营销方案、竞品分析' },
  ],
  documents: [
    { id: 1, title: '2026年Q3产品路线图', type: 'md', kb: '产品研发知识库', author: '张明', updatedAt: '2026-07-04 14:30', status: 'published', views: 234, tags: ['产品规划', 'Q3'], summary: '本文档概述了2026年第三季度的产品开发计划，包含核心功能迭代、技术债清理和架构升级三个维度...' },
    { id: 2, title: '微服务架构设计规范 v2.0', type: 'pdf', kb: '产品研发知识库', author: '李华', updatedAt: '2026-07-03 16:20', status: 'published', views: 456, tags: ['架构', '微服务'], summary: '基于APISIX网关的微服务架构设计规范，包含服务拆分原则、通信协议选型和部署策略...' },
    { id: 3, title: '企业知识库RAG系统技术方案', type: 'docx', kb: '产品研发知识库', author: '张明', updatedAt: '2026-07-02 10:15', status: 'published', views: 389, tags: ['RAG', 'AI', '技术方案'], summary: '基于LangGraph + LlamaIndex的Agentic RAG架构设计，支持多模态文档处理和MCP工具调用...' },
    { id: 4, title: '员工手册（2026修订版）', type: 'pdf', kb: '人力资源制度库', author: '孙莉', updatedAt: '2026-07-01 09:00', status: 'published', views: 678, tags: ['制度', '人力资源'], summary: '公司员工手册2026年修订版，包含考勤制度、薪酬福利、晋升通道和培训体系...' },
    { id: 5, title: '客户A智能客服系统部署案例', type: 'md', kb: '客户成功案例库', author: '王芳', updatedAt: '2026-06-30 15:45', status: 'published', views: 123, tags: ['案例', '智能客服'], summary: '某头部电商企业智能客服系统从方案设计到上线部署的完整案例，涵盖需求分析...' },
    { id: 6, title: '服务器故障排查SOP', type: 'md', kb: 'IT运维手册', author: '陈静', updatedAt: '2026-06-29 11:30', status: 'published', views: 234, tags: ['SOP', '运维'], summary: '服务器常见故障的标准排查流程，包含CPU/内存/磁盘/网络四大维度的诊断脚本...' },
    { id: 7, title: '差旅费用报销操作指南', type: 'pdf', kb: '财务报销指南', author: '赵伟', updatedAt: '2026-06-28 14:00', status: 'published', views: 567, tags: ['报销', '指南'], summary: '差旅费用报销的详细操作指南，包含报销标准、审批流程、票据要求和常见问题解答...' },
    { id: 8, title: '品牌视觉识别系统完整规范', type: 'pdf', kb: '市场营销素材库', author: '王芳', updatedAt: '2026-06-27 16:20', status: 'published', views: 345, tags: ['品牌', 'VI'], summary: '品牌视觉识别系统(VIS)完整规范文档，包含Logo使用规范、色彩体系、字体规范...' },
    { id: 9, title: 'API接口设计规范 v3.0', type: 'md', kb: '产品研发知识库', author: '李华', updatedAt: '2026-06-26 10:00', status: 'review', views: 89, tags: ['API', '规范'], summary: 'RESTful API设计规范第三版，新增GraphQL适配层和MCP工具协议支持...' },
    { id: 10, title: '新员工入职Onboarding清单', type: 'docx', kb: '人力资源制度库', author: '孙莉', updatedAt: '2026-06-25 09:30', status: 'published', views: 432, tags: ['入职', '清单'], summary: '新员工入职第一天到第三个月的完整Onboarding清单，包含账号开通、培训安排...' },
  ],
  chatSessions: [
    { id: 1, title: '产品Q3规划讨论', preview: '根据路线图，我们Q3需要完成...', time: '14:32', active: true, pinned: true },
    { id: 2, title: '微服务架构咨询', preview: 'APISIX和Kong的对比分析...', time: '13:15', pinned: false },
    { id: 3, title: '报销流程查询', preview: '差旅费报销需要哪些材料？', time: '11:08', pinned: false },
    { id: 4, title: 'RAG系统技术方案', preview: 'Agentic RAG的核心设计思路...', time: '昨天', pinned: true },
    { id: 5, title: '新员工入职指引', preview: '新员工第一天需要做什么？', time: '昨天', pinned: false },
    { id: 6, title: '竞品功能分析', preview: '对比三家竞品的知识管理能力...', time: '07-03', pinned: false },
    { id: 7, title: 'API规范v3讨论', preview: 'GraphQL适配层的设计方案...', time: '07-02', pinned: false },
    { id: 8, title: '服务器扩容方案', preview: '当前服务器配置无法满足...', time: '07-01', pinned: false },
  ],
  agents: [
    { id: 1, name: '通用问答Agent', desc: '基于企业知识库的通用问答助手', icon: '💬', type: 'qa', tools: ['知识检索', '文档总结'], status: 'active', calls: 3425, successRate: 94.2 },
    { id: 2, name: '报销助手Agent', desc: '自动处理报销流程和审批', icon: '💰', type: 'workflow', tools: ['表单填写', '审批流程', '票据识别'], status: 'active', calls: 892, successRate: 91.5 },
    { id: 3, name: 'IT运维Agent', desc: '服务器监控和故障排查', icon: '⚙️', type: 'action', tools: ['日志分析', '命令执行', '告警通知'], status: 'active', calls: 567, successRate: 88.3 },
    { id: 4, name: '文档审核Agent', desc: '自动审核文档质量和合规性', icon: '✅', type: 'qa', tools: ['内容审核', '格式检查', '查重比对'], status: 'active', calls: 1234, successRate: 96.8 },
    { id: 5, name: '新人导师Agent', desc: '为新员工提供入职指导', icon: '🎓', type: 'qa', tools: ['知识检索', '任务提醒', '进度追踪'], status: 'inactive', calls: 345, successRate: 92.1 },
  ],
  groups: [
    { id: 1, name: '产品研发群组', members: 8, lastMsg: '李华: 方案已更新，请review', time: '14:20', unread: 3 },
    { id: 2, name: 'Q3规划讨论组', members: 5, lastMsg: '张明: 下周一开评审会', time: '13:45', unread: 0 },
    { id: 3, name: '新人Onboarding', members: 12, lastMsg: '孙莉: 欢迎新同事加入！', time: '11:30', unread: 5 },
    { id: 4, name: '技术架构委员会', members: 6, lastMsg: '陈静: 架构文档已上传', time: '昨天', unread: 0 },
  ],
  tags: [
    { name: '产品规划', color: 'primary', count: 23 },
    { name: '技术方案', color: 'info', count: 45 },
    { name: '人力资源', color: 'success', count: 18 },
    { name: '财务制度', color: 'warning', count: 12 },
    { name: '运维SOP', color: 'danger', count: 34 },
    { name: '客户案例', color: 'primary', count: 28 },
    { name: 'API规范', color: 'info', count: 15 },
    { name: '品牌设计', color: 'neutral', count: 9 },
    { name: '入职指南', color: 'success', count: 7 },
    { name: '架构设计', color: 'primary', count: 31 },
  ],
  stats: {
    totalDocs: 1019,
    totalKB: 6,
    totalUsers: 128,
    totalQueries: 8542,
    todayQueries: 234,
    avgResponseTime: '1.2s',
    knowledgeCoverage: 87.3,
    satisfactionRate: 92.5,
  },
  healthMetrics: [
    { name: '知识覆盖率', value: 87.3, target: 90, status: 'warning' },
    { name: '内容新鲜度', value: 92.1, target: 85, status: 'success' },
    { name: '引用准确率', value: 94.8, target: 95, status: 'warning' },
    { name: '用户活跃度', value: 78.5, target: 80, status: 'warning' },
    { name: '文档完整度', value: 89.2, target: 85, status: 'success' },
    { name: '检索响应速度', value: 96.3, target: 90, status: 'success' },
  ],
  knowledgeGaps: [
    { id: 1, topic: 'AI Agent开发实战指南', searches: 234, status: 'high', desc: '用户频繁搜索AI Agent开发相关内容，但现有文档仅有理论介绍，缺乏实战案例和代码示例。', suggestion: '建议补充LangGraph实战教程、Agent开发最佳实践等文档' },
    { id: 2, topic: '多租户架构设计方案', searches: 189, status: 'high', desc: 'SaaS产品多租户架构相关搜索量较大，但知识库中仅有一篇概述性文档，缺少详细设计。', suggestion: '需要补充数据隔离方案、租户管理、计费架构等专题文档' },
    { id: 3, topic: 'VLM视觉模型部署', searches: 156, status: 'medium', desc: '视觉语言模型部署相关内容覆盖不足，用户需要从基础概念到生产部署的完整指南。', suggestion: '建议添加VLM选型对比、API调用示例、本地部署教程' },
    { id: 4, topic: 'MCP协议接入指南', searches: 134, status: 'medium', desc: 'MCP(Model Context Protocol)工具协议接入文档较少，用户不清楚如何自定义工具。', suggestion: '需要编写MCP协议入门、工具开发指南、调试技巧' },
    { id: 5, topic: '知识图谱可视化', searches: 98, status: 'low', desc: '知识图谱可视化的交互设计和前端实现方案文档较少。', suggestion: '可补充D3.js/Vis.js可视化方案和交互设计规范' },
    { id: 6, topic: 'SSO集成最佳实践', searches: 87, status: 'low', desc: '单点登录集成相关文档较分散，缺少统一的最佳实践指南。', suggestion: '整合SAML/OAuth/OIDC三种方案的对比和实施指南' },
  ],
  feedbacks: [
    { id: 1, user: '李华', type: 'bug', content: '搜索结果排序不太准确，关键词匹配优先级需要优化', status: 'open', time: '2026-07-04 10:30', priority: 'high' },
    { id: 2, user: '王芳', type: 'feature', content: '希望支持文档版本对比功能，方便查看修改历史', status: 'planned', time: '2026-07-03 16:20', priority: 'medium' },
    { id: 3, user: '刘强', type: 'improvement', content: 'AI对话的引用来源可以更明显一些', status: 'resolved', time: '2026-07-02 09:15', priority: 'low' },
    { id: 4, user: '陈静', type: 'bug', content: '知识图谱页面在Safari浏览器下渲染异常', status: 'open', time: '2026-07-01 14:00', priority: 'high' },
    { id: 5, user: '赵伟', type: 'feature', content: '希望增加团队知识贡献排行榜', status: 'planned', time: '2026-06-30 11:45', priority: 'medium' },
    { id: 6, user: '孙莉', type: 'improvement', content: '文档上传后解析速度较慢，希望支持异步处理', status: 'resolved', time: '2026-06-29 15:30', priority: 'low' },
  ],
  auditItems: [
    { id: 1, doc: 'API接口设计规范 v3.0', submitter: '李华', submitTime: '2026-07-04 09:00', status: 'pending', type: 'new', reviewer: '张明' },
    { id: 2, doc: '2026年Q3产品路线图', submitter: '张明', submitTime: '2026-07-03 14:30', status: 'approved', type: 'update', reviewer: '陈静', reviewTime: '2026-07-03 16:00' },
    { id: 3, doc: '微服务架构设计规范 v2.0', submitter: '李华', submitTime: '2026-07-02 10:00', status: 'approved', type: 'new', reviewer: '张明', reviewTime: '2026-07-02 15:20' },
    { id: 4, doc: '知识库迁移指南', submitter: '王芳', submitTime: '2026-07-01 16:00', status: 'rejected', type: 'new', reviewer: '张明', reviewTime: '2026-07-01 17:30', reason: '内容不够详细，需要补充迁移步骤' },
    { id: 5, doc: '容器化部署手册', submitter: '陈静', submitTime: '2026-06-30 11:00', status: 'pending', type: 'update', reviewer: '张明' },
  ],
  apiKeys: [
    { id: 1, name: '前端应用', key: 'ekb_pk_a1b2c3d4e5f6...', scope: '读写', created: '2026-06-01', lastUsed: '刚刚', status: 'active' },
    { id: 2, name: '移动端App', key: 'ekb_pk_x7y8z9w0v1u2...', scope: '读写', created: '2026-05-15', lastUsed: '2小时前', status: 'active' },
    { id: 3, name: '数据分析平台', key: 'ekb_pk_m3n4o5p6q7r8...', scope: '只读', created: '2026-04-20', lastUsed: '昨天', status: 'active' },
    { id: 4, name: '旧版API(已废弃)', key: 'ekb_pk_t9s0r1u2v3w4...', scope: '只读', created: '2026-01-10', lastUsed: '30天前', status: 'inactive' },
  ],
  llmConfigs: [
    { provider: 'OpenAI', model: 'GPT-4o', apiKey: 'sk-***...***7a3b', status: 'active', usage: '45.2K tokens', cost: '$128.5' },
    { provider: 'Anthropic', model: 'Claude-3.5-Sonnet', apiKey: 'sk-ant-***...***9f2c', status: 'active', usage: '23.8K tokens', cost: '$95.2' },
    { provider: 'Qwen', model: 'Qwen-VL-Max', apiKey: 'sk-***...***3d8e', status: 'active', usage: '12.1K images', cost: '¥456.0' },
    { provider: 'DeepSeek', model: 'DeepSeek-V3', apiKey: 'sk-***...***5b1a', status: 'inactive', usage: '0 tokens', cost: '¥0.0' },
  ],
  timelineEvents: [
    { date: '2026-07-04 14:30', title: '产品路线图文档发布', type: 'success', content: '张明 发布了《2026年Q3产品路线图》，已被234人查看' },
    { date: '2026-07-04 10:00', title: 'API规范v3提交审核', type: 'warning', content: '李华 提交了《API接口设计规范 v3.0》的审核请求，等待 张明 审核' },
    { date: '2026-07-03 16:20', title: '微服务架构文档更新', type: 'success', content: '李华 更新了《微服务架构设计规范 v2.0》，新增APISIX网关章节' },
    { date: '2026-07-03 09:15', title: '知识缺口预警', type: 'danger', content: '系统检测到「AI Agent开发实战指南」搜索量异常增长(234次)，建议补充相关文档' },
    { date: '2026-07-02 15:00', title: 'RAG技术方案文档发布', type: 'success', content: '张明 发布了《企业知识库RAG系统技术方案》，已被389人查看' },
    { date: '2026-07-02 10:00', title: '新知识库创建', type: 'success', content: '陈静 创建了新知识库「IT运维手册」，包含203篇文档' },
    { date: '2026-07-01 14:00', title: '文档审核未通过', type: 'danger', content: '张明 驳回了《知识库迁移指南》审核，原因：内容不够详细，需要补充迁移步骤' },
    { date: '2026-06-30 11:00', title: '用户反馈处理', type: 'success', content: '已处理用户反馈「AI对话引用来源不明显」，优化了引用标注的视觉设计' },
  ],
};

// === 工具函数 ===
const Utils = {
  icon(name, size) {
    const svg = Icons[name] || '';
    if (size && svg) { return svg.replace(/width="20"/g, `width="${size}"`).replace(/height="20"/g, `height="${size}"`); }
    return svg;
  },
  formatDate(d) {
    if (typeof d === 'string') return d;
    const date = new Date(d);
    return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
  },
  truncate(s, len) { return s.length > len ? s.slice(0, len) + '...' : s; },
  fileIcon(type) {
    const icons = { pdf: '📄', doc: '📝', docx: '📝', md: '📋', xlsx: '📊', pptx: '📽️', txt: '📃', img: '🖼️', video: '🎬' };
    return icons[type] || '📄';
  },
  fileTypeLabel(type) {
    const labels = { pdf: 'PDF', doc: 'Word', docx: 'Word', md: 'Markdown', xlsx: 'Excel', pptx: 'PPT', txt: '文本', img: '图片', video: '视频' };
    return labels[type] || type.toUpperCase();
  },
  avatarColor(name) {
    const colors = ['#4B3FE3', '#00B884', '#FF9500', '#FF3B5C', '#2196F3', '#9C27B0', '#00BCD4', '#E91E63'];
    let hash = 0;
    for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
    return colors[Math.abs(hash) % colors.length];
  },
  randomId() { return 'id_' + Math.random().toString(36).substr(2, 9); },
  escape(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); },
};

// === 核心应用 ===
const App = {
  pages: {},
  currentRoute: '',

  registerPage(route, config) {
    this.pages[route] = config;
  },

  navigate(route) {
    window.location.hash = '#' + route;
  },

  getRoute() {
    const hash = window.location.hash.slice(1) || 'home';
    return hash;
  },

  getRouteParts() {
    const hash = this.getRoute();
    return hash.split('/');
  },

  render() {
    const route = this.getRoute();
    const parts = this.getRouteParts();
    const mainRoute = parts[0];

    // 查找匹配的页面
    let page = this.pages[route] || this.pages[mainRoute] || this.pages['home'];

    if (!page) {
      document.getElementById('app').innerHTML = '<div class="fullscreen-page"><div class="empty-state"><div class="empty-state-icon">🔍</div><div class="empty-state-title">页面未找到</div><div class="empty-state-desc">请检查URL是否正确</div></div></div>';
      return;
    }

    this.currentRoute = route;

    // 全屏页面（登录等）
    if (page.fullscreen) {
      document.getElementById('app').innerHTML = page.render(parts.slice(1));
      if (page.init) page.init(parts.slice(1));
      return;
    }

    // 标准布局（侧边栏 + 顶栏 + 内容）
    const content = page.render(parts.slice(1));
    document.getElementById('app').innerHTML = this.renderShell(route, page, content);
    this.bindShellEvents();
    if (page.init) page.init(parts.slice(1));
  },

  renderShell(currentRoute, page, content) {
    const navGroups = this.getNavConfig();
    const parts = currentRoute.split('/');
    const mainRoute = parts[0];

    let navHtml = '';
    navGroups.forEach(group => {
      navHtml += `<div class="sidebar-group">`;
      if (group.label) {
        navHtml += `<div class="sidebar-group-label">${group.label}</div>`;
      }
      group.items.forEach(item => {
        const isActive = mainRoute === item.route.split('/')[0] ||
          (parts.length > 1 && currentRoute.startsWith(item.route.split('/')[0]));
        navHtml += `
          <a href="#${item.route}" class="sidebar-item ${isActive ? 'active' : ''}">
            <span class="sidebar-item-icon">${Utils.icon(item.icon)}</span>
            <span>${item.label}</span>
            ${item.badge ? `<span class="sidebar-item-badge">${item.badge}</span>` : ''}
          </a>
        `;
      });
      navHtml += `</div>`;
    });

    return `
      <div class="app-shell">
        <aside class="sidebar">
          <div class="sidebar-logo">
            <div class="sidebar-logo-icon">🧠</div>
            <div class="sidebar-logo-text">企业知识库</div>
          </div>
          <nav class="sidebar-nav">
            ${navHtml}
          </nav>
          <div class="sidebar-user" onclick="App.navigate('settings/system')">
            <div class="avatar avatar-md" style="background: ${Utils.avatarColor(Mock.currentUser.name)}">${Mock.currentUser.avatar}</div>
            <div class="sidebar-user-info">
              <div class="sidebar-user-name">${Mock.currentUser.name}</div>
              <div class="sidebar-user-role">${Mock.currentUser.role} · ${Mock.currentUser.dept}</div>
            </div>
            ${Utils.icon('settings', 16)}
          </div>
        </aside>
        <div class="main-area">
          <header class="topbar">
            <div class="topbar-breadcrumb">
              ${this.renderBreadcrumb(mainRoute, page)}
            </div>
            <div class="topbar-spacer"></div>
            <div class="topbar-actions">
              <div class="topbar-search">
                <span class="topbar-search-icon">${Utils.icon('search', 16)}</span>
                <input type="text" placeholder="搜索知识库..." onkeydown="if(event.key==='Enter')App.navigate('knowledge/search')" />
              </div>
              <button class="icon-btn" onclick="App.toast('暂无新通知', 'info')">${Utils.icon('bell')}<span class="dot"></span></button>
              <button class="btn btn-primary btn-sm" onclick="App.navigate('chat')">
                ${Utils.icon('sparkles', 16)}
                <span>AI 对话</span>
              </button>
            </div>
          </header>
          <main class="content-area">
            <div class="page-container fade-in">
              ${content}
            </div>
          </main>
        </div>
      </div>
    `;
  },

  renderBreadcrumb(route, page) {
    const breadcrumbMap = {
      home: [{ label: '工作台', route: 'home' }],
      chat: [{ label: 'AI 对话', route: 'chat' }, { label: '对话', route: 'chat' }],
      'chat/history': [{ label: 'AI 对话', route: 'chat' }, { label: '对话历史', route: 'chat/history' }],
      'chat/agent': [{ label: 'AI 对话', route: 'chat' }, { label: 'Agent 详情', route: 'chat/agent' }],
      knowledge: [{ label: '知识库', route: 'knowledge' }, { label: '知识首页', route: 'knowledge' }],
      'knowledge/search': [{ label: '知识库', route: 'knowledge' }, { label: '搜索', route: 'knowledge/search' }],
      'knowledge/qa': [{ label: '知识库', route: 'knowledge' }, { label: '问答社区', route: 'knowledge/qa' }],
      'knowledge/graph': [{ label: '知识库', route: 'knowledge' }, { label: '知识图谱', route: 'knowledge/graph' }],
      'knowledge/timeline': [{ label: '知识库', route: 'knowledge' }, { label: '时间线', route: 'knowledge/timeline' }],
      'knowledge/doc': [{ label: '知识库', route: 'knowledge' }, { label: '文档详情', route: 'knowledge/doc' }],
      'manage/kb': [{ label: '知识管理', route: 'manage/kb' }, { label: '知识库管理', route: 'manage/kb' }],
      'manage/upload': [{ label: '知识管理', route: 'manage/kb' }, { label: '文档上传', route: 'manage/upload' }],
      'manage/editor': [{ label: '知识管理', route: 'manage/kb' }, { label: '协同编辑', route: 'manage/editor' }],
      'manage/minutes': [{ label: '知识管理', route: 'manage/kb' }, { label: '会议纪要', route: 'manage/minutes' }],
      'manage/gaps': [{ label: '知识管理', route: 'manage/kb' }, { label: '知识缺口', route: 'manage/gaps' }],
      admin: [{ label: '运营治理', route: 'admin' }, { label: '运营看板', route: 'admin' }],
      'admin/health': [{ label: '运营治理', route: 'admin' }, { label: '健康度看板', route: 'admin/health' }],
      'admin/audit': [{ label: '运营治理', route: 'admin' }, { label: '审核工作流', route: 'admin/audit' }],
      'admin/users': [{ label: '运营治理', route: 'admin' }, { label: '用户权限', route: 'admin/users' }],
      'admin/tags': [{ label: '运营治理', route: 'admin' }, { label: '标签管理', route: 'admin/tags' }],
      'admin/reports': [{ label: '运营治理', route: 'admin' }, { label: '使用报表', route: 'admin/reports' }],
      'admin/feedback': [{ label: '运营治理', route: 'admin' }, { label: '反馈管理', route: 'admin/feedback' }],
      'settings/tenant': [{ label: '系统设置', route: 'settings/tenant' }, { label: '租户管理', route: 'settings/tenant' }],
      'settings/api': [{ label: '系统设置', route: 'settings/tenant' }, { label: 'API 密钥', route: 'settings/api' }],
      'settings/llm': [{ label: '系统设置', route: 'settings/tenant' }, { label: 'LLM/VLM 配置', route: 'settings/llm' }],
      'settings/system': [{ label: '系统设置', route: 'settings/tenant' }, { label: '系统设置', route: 'settings/system' }],
      'scenes/onboarding': [{ label: '场景应用', route: 'scenes/onboarding' }, { label: '入职助手', route: 'scenes/onboarding' }],
      'scenes/it-helpdesk': [{ label: '场景应用', route: 'scenes/onboarding' }, { label: 'IT 工单', route: 'scenes/it-helpdesk' }],
    };

    const crumbs = breadcrumbMap[route] || [{ label: page.title || '页面', route }];
    let html = '';
    crumbs.forEach((c, i) => {
      if (i > 0) html += `<span class="topbar-breadcrumb-sep">/</span>`;
      html += `<a href="#${c.route}">${c.label}</a>`;
    });
    return html;
  },

  getNavConfig() {
    return [
      {
        label: '',
        items: [
          { label: '工作台', route: 'home', icon: 'home' },
        ]
      },
      {
        label: 'AI 能力',
        items: [
          { label: 'AI 对话', route: 'chat', icon: 'chat' },
          { label: '对话历史', route: 'chat/history', icon: 'clock' },
          { label: 'Agent 列表', route: 'chat/agent', icon: 'sparkles' },
        ]
      },
      {
        label: '知识',
        items: [
          { label: '知识首页', route: 'knowledge', icon: 'book' },
          { label: '知识搜索', route: 'knowledge/search', icon: 'search' },
          { label: '问答社区', route: 'knowledge/qa', icon: 'message' },
          { label: '知识图谱', route: 'knowledge/graph', icon: 'graph' },
          { label: '时间线', route: 'knowledge/timeline', icon: 'clock' },
        ]
      },
      {
        label: '管理',
        items: [
          { label: '知识库管理', route: 'manage/kb', icon: 'folder' },
          { label: '文档上传', route: 'manage/upload', icon: 'upload' },
          { label: '协同编辑', route: 'manage/editor', icon: 'edit' },
          { label: '会议纪要', route: 'manage/minutes', icon: 'video' },
          { label: '知识缺口', route: 'manage/gaps', icon: 'alert', badge: '6' },
        ]
      },
      {
        label: '治理',
        items: [
          { label: '运营看板', route: 'admin', icon: 'chart' },
          { label: '健康度', route: 'admin/health', icon: 'shield' },
          { label: '审核工作流', route: 'admin/audit', icon: 'check', badge: '2' },
          { label: '用户权限', route: 'admin/users', icon: 'users' },
          { label: '标签管理', route: 'admin/tags', icon: 'tag' },
          { label: '使用报表', route: 'admin/reports', icon: 'trending' },
          { label: '反馈管理', route: 'admin/feedback', icon: 'message', badge: '4' },
        ]
      },
      {
        label: '系统',
        items: [
          { label: '租户管理', route: 'settings/tenant', icon: 'building' },
          { label: 'API 密钥', route: 'settings/api', icon: 'key' },
          { label: 'LLM/VLM', route: 'settings/llm', icon: 'cpu' },
          { label: '系统设置', route: 'settings/system', icon: 'settings' },
        ]
      },
      {
        label: '场景',
        items: [
          { label: '入职助手', route: 'scenes/onboarding', icon: 'star' },
          { label: 'IT 工单', route: 'scenes/it-helpdesk', icon: 'help' },
        ]
      },
    ];
  },

  bindShellEvents() {
    // 顶部搜索框快捷键
    const searchInput = document.querySelector('.topbar-search input');
    if (searchInput) {
      searchInput.addEventListener('keydown', (e) => {
        if (e.key === '/') { e.preventDefault(); searchInput.focus(); }
      });
    }
  },

  toast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const icons = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
    toast.innerHTML = `<span>${icons[type] || 'ℹ'}</span><span>${message}</span>`;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(100%)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  },

  modal(options) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal ${options.size || ''}" onclick="event.stopPropagation()">
        <div class="modal-header">
          <div class="modal-title">${options.title || ''}</div>
          <button class="icon-btn" onclick="this.closest('.modal-overlay').remove()">${Utils.icon('close')}</button>
        </div>
        <div class="modal-body">${options.body || ''}</div>
        ${options.footer !== false ? `<div class="modal-footer">
          <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">${options.cancelText || '取消'}</button>
          ${options.confirmText ? `<button class="btn btn-primary" id="modalConfirm">${options.confirmText}</button>` : ''}
        </div>` : ''}
      </div>
    `;
    document.body.appendChild(overlay);
    setTimeout(() => overlay.classList.add('active'), 10);

    overlay.addEventListener('click', () => overlay.remove());

    if (options.onConfirm) {
      const btn = overlay.querySelector('#modalConfirm');
      if (btn) btn.addEventListener('click', () => {
        options.onConfirm(overlay);
      });
    }

    if (options.onMount) options.onMount(overlay);
    return overlay;
  },

  init() {
    window.addEventListener('hashchange', () => this.render());
    this.render();
  },
};

// === 全局快捷键 ===
document.addEventListener('keydown', (e) => {
  // Cmd/Ctrl + K → 搜索
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault();
    App.navigate('knowledge/search');
  }
});
