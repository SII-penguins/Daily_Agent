/** Run from a materialized private checkout; never prints report bytes. */
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
const root = process.cwd();
const inventory = JSON.parse(await readFile(resolve(root,'.sites-runtime/archive-inventory.json'),'utf8'));
const directories = process.argv.slice(2);
if (!directories.length) directories.push('public');
for (const directory of directories) {
  for (const file of inventory.files) {
    const bytes = await readFile(resolve(root,directory,file.path));
    if (bytes.length !== file.bytes || createHash('sha256').update(bytes).digest('hex') !== file.sha256) throw new Error('Retained archive file is missing or changed');
  }
}
console.log(JSON.stringify({archive_files:inventory.files.length,checked_directories:directories,archive_unchanged:true}));
