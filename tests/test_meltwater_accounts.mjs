import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createAccountStore, emailFromToken, accountIdFromToken } from '../meltwater-studio/ui/accounts.mjs';

test('JWT email detection accepts email claims and no other strings', () => {
  const token = `a.${Buffer.from(JSON.stringify({email:'researcher@example.org'})).toString('base64url')}.z`;
  assert.equal(emailFromToken(`Bearer ${token}`), 'researcher@example.org');
  assert.equal(emailFromToken('invalid'), '');
  const accountToken=`a.${Buffer.from(JSON.stringify({company:{_id:'62bd23a40490b900113ddaca'}})).toString('base64url')}.z`;
  assert.equal(accountIdFromToken(accountToken), '62bd23a40490b900113ddaca');
});

test('unlogged account can save an email and be deleted with its profile', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'suyuan-mw-'));
  try {
    const key = 'a1b2c3d4e5f6';
    const metadataFile = path.join(root, 'accounts.json');
    const profileRoot = path.join(root, 'profiles');
    fs.mkdirSync(path.join(profileRoot, key), {recursive:true});
    fs.writeFileSync(path.join(profileRoot, key, 'test.txt'), 'private browser data');
    fs.writeFileSync(metadataFile, JSON.stringify([{key,label:'未登录账号',accountId:''}]));
    const store = createAccountStore({api:{},cdpModule:{},metadataFile,defaultAccountId:'',profileRoot});
    assert.equal(store.list().find(a=>a.key===key).status, '待连接');
    assert.equal(store.setEmail(key, 'owner@example.org').email, 'owner@example.org');
    assert.throws(()=>store.setEmail(key, 'invalid'), /邮箱格式/);
    const restored = createAccountStore({api:{},cdpModule:{},metadataFile,defaultAccountId:'',profileRoot});
    assert.equal(restored.list().find(a=>a.key===key).email, 'owner@example.org');
    assert.deepEqual(await store.remove(key), {removed:true,key});
    assert.equal(fs.existsSync(path.join(profileRoot,key)), false);
    assert.equal(store.list().some(a=>a.key===key), false);
    await assert.rejects(store.remove('default'), /不能删除/);
  } finally { fs.rmSync(root, {recursive:true,force:true}); }
});
