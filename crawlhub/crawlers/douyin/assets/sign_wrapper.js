/**
 * Wrapper script to call sign() from dy_live_sign.js via Node.js subprocess.
 * Usage: node sign_wrapper.js <roomId> <userId>
 * Output: JSON string with the signature result.
 */
const path = require('path');
const fs = require('fs');

const args = process.argv.slice(2);
if (args.length < 2) {
    console.error(JSON.stringify({ error: 'Usage: node sign_wrapper.js <roomId> <userId>' }));
    process.exit(1);
}

const roomId = args[0];
const userId = args[1];

// dy_live_sign.js defines `function sign(roomId, userId)` at the top level.
// We need to load it as a proper module so require() works correctly.
// Strategy: create a temp file that requires the original and exports sign.
const signPath = path.join(__dirname, 'dy_live_sign.js');
const tmpPath = path.join(__dirname, '_tmp_sign_' + process.pid + '.js');

// Read the original file and append module.exports
const originalCode = fs.readFileSync(signPath, 'utf-8');
fs.writeFileSync(tmpPath, originalCode + '\nmodule.exports = sign;\n', 'utf-8');

try {
    const signFn = require(tmpPath);
    const result = signFn(roomId, userId);
    process.stdout.write(JSON.stringify({ signature: result }) + '\n');
} catch (e) {
    process.stderr.write(JSON.stringify({ error: e.message }) + '\n');
    process.exit(1);
} finally {
    try { fs.unlinkSync(tmpPath); } catch (e) {}
}