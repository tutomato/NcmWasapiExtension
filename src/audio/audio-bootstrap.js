'use strict';
// Bootstrap only the enumerator. No particular device has to be connected.
const ole = Process.getModuleByName('combase.dll');
function guid(s) {
  const p = Memory.alloc(16), hex = s.replaceAll('-', ''), b = [];
  for (let i = 0; i < 32; i += 2) b.push(parseInt(hex.slice(i, i + 2), 16));
  p.writeByteArray([b[3], b[2], b[1], b[0], b[5], b[4], b[7], b[6], ...b.slice(8)]);
  return p;
}
function method(p, n, ret, args) {
  return new NativeFunction(p.readPointer().add(n * Process.pointerSize).readPointer(), ret, args);
}
new NativeFunction(ole.getExportByName('CoInitializeEx'), 'int', ['pointer', 'uint'])(ptr(0), 0);
const create = new NativeFunction(ole.getExportByName('CoCreateInstance'), 'int',
  ['pointer', 'pointer', 'uint', 'pointer', 'pointer']);
const out = Memory.alloc(Process.pointerSize);
const hr = create(guid('bcde0395-e52f-467c-8e3d-c4579291692e'), ptr(0), 1,
  guid('a95664d2-9614-4f35-a746-de8db63617e6'), out);
if (hr < 0) throw Error('Audio enumerator ' + hr);
const en = out.readPointer();
