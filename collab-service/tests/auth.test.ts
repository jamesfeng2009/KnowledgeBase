/**
 * @file auth.test.ts
 * @description 协作服务鉴权/授权的回归测试（node --test，无需真实 PostgreSQL）。
 *
 * 锁定两个 P1 安全修复：
 *  1. ``verifyJwt`` 必须**真的验签**，而不是 base64 解码 payload 就放行 ——
 *     修复前任何人都能伪造 ``{"sub":"任意用户"}`` 进任意文档；
 *  2. ``authorizeDocument`` 必须校验文档归属/租户/密级/成员角色 —— 修复前完全没有
 *     这层校验（IDOR）。
 *
 * 数据库用桩对象替代：``authorizeDocument`` 只依赖 ``pool.query(sql, params)``
 * 的返回值结构，用假 rows 就能覆盖全部分支。
 */

import { strict as assert } from 'node:assert';
import { createHmac } from 'node:crypto';
import test from 'node:test';
import type { Pool } from 'pg';

import {
  authorizeDocument,
  getCookieToken,
  verifyJwt,
} from '../src/auth.js';

const SECRET = 'test-secret-key';
const DOC_ID = '11111111-1111-1111-1111-111111111111';
const USER_ID = '22222222-2222-2222-2222-222222222222';

/** 用 HS256 造一个 token（模拟后端 app/utils/crypto.py 的签发） */
function sign(
  payload: Record<string, unknown>,
  secret = SECRET,
  alg = 'HS256'
): string {
  const header = Buffer.from(JSON.stringify({ alg, typ: 'JWT' })).toString('base64url');
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const sig = createHmac('sha256', secret).update(`${header}.${body}`).digest('base64url');
  return `${header}.${body}.${sig}`;
}

const nowSec = Math.floor(Date.now() / 1000);

// ======================================================================
// verifyJwt
// ======================================================================

test('合法 token 通过验签并返回 payload', () => {
  const token = sign({ sub: USER_ID, role: 'editor', exp: nowSec + 600 });
  const result = verifyJwt(token, SECRET);
  assert.equal(result.ok, true);
  if (result.ok) assert.equal(result.payload.sub, USER_ID);
});

test('篡改 payload 的 token 被拒绝', () => {
  const token = sign({ sub: USER_ID, exp: nowSec + 600 });
  const [header, , sig] = token.split('.');
  const forgedBody = Buffer.from(
    JSON.stringify({ sub: 'attacker', exp: nowSec + 600 })
  ).toString('base64url');
  const result = verifyJwt(`${header}.${forgedBody}.${sig}`, SECRET);
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.reason, 'bad_signature');
});

test('用错误密钥签发的 token 被拒绝', () => {
  const token = sign({ sub: USER_ID, exp: nowSec + 600 }, 'another-secret');
  const result = verifyJwt(token, SECRET);
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.reason, 'bad_signature');
});

test('alg=none 的 token 被拒绝（算法混淆攻击）', () => {
  const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
  const body = Buffer.from(JSON.stringify({ sub: 'attacker' })).toString('base64url');
  const result = verifyJwt(`${header}.${body}.`, SECRET);
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.reason, /unsupported_alg/);
});

test('RS256 等非 HS256 算法一律拒绝', () => {
  const result = verifyJwt(sign({ sub: USER_ID }, SECRET, 'RS256'), SECRET);
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.reason, /unsupported_alg/);
});

test('过期 token 被拒绝', () => {
  const token = sign({ sub: USER_ID, exp: nowSec - 10 });
  const result = verifyJwt(token, SECRET);
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.reason, 'token_expired');
});

test('未到期但 nbf 未生效的 token 被拒绝', () => {
  const token = sign({ sub: USER_ID, exp: nowSec + 600, nbf: nowSec + 600 });
  const result = verifyJwt(token, SECRET);
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.reason, 'token_not_yet_valid');
});

test('SECRET_KEY 缺失时 fail-closed', () => {
  const token = sign({ sub: USER_ID, exp: nowSec + 600 });
  const result = verifyJwt(token, undefined);
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.reason, /server_misconfigured/);
});

test('无 token / 结构错误被拒绝', () => {
  assert.equal(verifyJwt(null, SECRET).ok, false);
  assert.equal(verifyJwt('', SECRET).ok, false);
  assert.equal(verifyJwt('a.b', SECRET).ok, false);
  assert.equal(verifyJwt('not-a-jwt', SECRET).ok, false);
});

// ======================================================================
// getCookieToken
// ======================================================================

test('从 Cookie 头中提取 access_token（含多 Cookie 场景）', () => {
  assert.equal(
    getCookieToken('foo=1; access_token=abc.def.ghi; bar=2'),
    'abc.def.ghi'
  );
  assert.equal(getCookieToken('access_token=xyz'), 'xyz');
  assert.equal(getCookieToken('other=1'), null);
  assert.equal(getCookieToken(undefined), null);
});

test('只认 Cookie，不认 URL query（token 不得进访问日志）', () => {
  // 该函数签名只接受 cookie 头；这里断言它不会把整个 URL 当 cookie 解析
  assert.equal(getCookieToken('?token=abc'), null);
});

// ======================================================================
// authorizeDocument
// ======================================================================

/** 造一个只实现 query 的假连接池 */
function fakePool(row: Record<string, unknown> | undefined): Pool {
  return {
    query: async () => ({ rows: row ? [row] : [] }),
  } as unknown as Pool;
}

/** 一条"知识库 owner 的普通文档"记录 */
function baseRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: DOC_ID,
    deleted_at: null,
    classification: 'internal',
    doc_tenant_id: null,
    kb_owner_id: USER_ID,
    kb_visibility: 'private',
    kb_dept_id: null,
    user_role: 'editor',
    user_clearance: 'internal',
    user_tenant_id: null,
    user_dept_id: null,
    member_role: null,
    ...overrides,
  };
}

test('知识库 owner 可写', async () => {
  const result = await authorizeDocument(fakePool(baseRow()), DOC_ID, USER_ID, 'write');
  assert.equal(result.allowed, true);
});

test('PG 不可用时 fail-closed', async () => {
  const result = await authorizeDocument(null, DOC_ID, USER_ID, 'write');
  assert.equal(result.allowed, false);
  if (!result.allowed) assert.equal(result.reason, 'authorization_unavailable');
});

test('非法 docId / userId 直接拒绝（不进 SQL 的 uuid 转换）', async () => {
  const pool = fakePool(baseRow());
  for (const bad of ['default', 'abc', '']) {
    const r = await authorizeDocument(pool, bad, USER_ID, 'write');
    assert.equal(r.allowed, false);
  }
  const r = await authorizeDocument(pool, DOC_ID, 'not-a-uuid', 'write');
  assert.equal(r.allowed, false);
  if (!r.allowed) assert.equal(r.reason, 'invalid_user_id');
});

test('跨租户访问被拒绝（IDOR）', async () => {
  const row = baseRow({
    doc_tenant_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    user_tenant_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
  });
  const result = await authorizeDocument(fakePool(row), DOC_ID, USER_ID, 'write');
  assert.equal(result.allowed, false);
  if (!result.allowed) assert.equal(result.reason, 'tenant_mismatch');
});

test('文档不存在 / 已软删除 / 用户已删除 均拒绝', async () => {
  const notFound = await authorizeDocument(fakePool(undefined), DOC_ID, USER_ID);
  assert.equal(notFound.allowed, false);

  const deleted = await authorizeDocument(
    fakePool(baseRow({ deleted_at: new Date() })),
    DOC_ID,
    USER_ID
  );
  assert.equal(deleted.allowed, false);

  const noUser = await authorizeDocument(
    fakePool(baseRow({ user_role: null })),
    DOC_ID,
    USER_ID
  );
  assert.equal(noUser.allowed, false);
});

test('viewer 成员不能写协同（但可以读）', async () => {
  const row = baseRow({
    kb_owner_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
    member_role: 'viewer',
    kb_visibility: 'private',
  });
  const write = await authorizeDocument(fakePool(row), DOC_ID, USER_ID, 'write');
  assert.equal(write.allowed, false);
  if (!write.allowed) assert.match(String(write.reason), /forbidden/);

  const read = await authorizeDocument(fakePool(row), DOC_ID, USER_ID, 'read');
  assert.equal(read.allowed, true);
});

test('editor 成员可写', async () => {
  const row = baseRow({
    kb_owner_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
    member_role: 'editor',
  });
  const result = await authorizeDocument(fakePool(row), DOC_ID, USER_ID, 'write');
  assert.equal(result.allowed, true);
});

test('无任何关系的用户不能读也不能写', async () => {
  const row = baseRow({
    kb_owner_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
    member_role: null,
    kb_visibility: 'private',
  });
  const pool = fakePool(row);
  assert.equal((await authorizeDocument(pool, DOC_ID, USER_ID, 'write')).allowed, false);
  assert.equal((await authorizeDocument(pool, DOC_ID, USER_ID, 'read')).allowed, false);
});

test('public 库可读但不可写（写仍需 owner/成员）', async () => {
  const row = baseRow({
    kb_owner_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
    member_role: null,
    kb_visibility: 'public',
  });
  const pool = fakePool(row);
  assert.equal((await authorizeDocument(pool, DOC_ID, USER_ID, 'read')).allowed, true);
  assert.equal((await authorizeDocument(pool, DOC_ID, USER_ID, 'write')).allowed, false);
});

test('admin 角色不受密级限制', async () => {
  const row = baseRow({
    kb_owner_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
    member_role: null,
    user_role: 'admin',
    classification: 'secret',
    user_clearance: 'public',
  });
  const result = await authorizeDocument(fakePool(row), DOC_ID, USER_ID, 'write');
  assert.equal(result.allowed, true);
});

test('密级不足时拒绝（成员也不行）', async () => {
  const row = baseRow({
    kb_owner_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
    member_role: 'editor',
    classification: 'secret',
    user_clearance: 'internal',
  });
  const result = await authorizeDocument(fakePool(row), DOC_ID, USER_ID, 'write');
  assert.equal(result.allowed, false);
  if (!result.allowed) assert.equal(result.reason, 'insufficient_clearance');
});
