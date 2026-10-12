import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"

const localManifest = readFileSync(new URL("../manifest.xml", import.meta.url), "utf8")
const publicManifest = readFileSync(new URL("../manifest.render.xml", import.meta.url), "utf8")
const macInstaller = readFileSync(new URL("../../../tools/word-installers/mac/install-ccitecheck.command", import.meta.url), "utf8")
const windowsInstaller = readFileSync(new URL("../../../tools/word-installers/windows/install-ccitecheck.ps1", import.meta.url), "utf8")

function addinId(manifest) {
  return manifest.match(/<Id>([^<]+)<\/Id>/)?.[1]
}

test("local and public Word add-ins use distinct identities", () => {
  assert.ok(addinId(localManifest))
  assert.ok(addinId(publicManifest))
  assert.notEqual(addinId(localManifest), addinId(publicManifest))
})

test("public Word add-in only references the Render origin", () => {
  assert.match(publicManifest, /https:\/\/cciteheck-api\.onrender\.com\/taskpane\.html/)
  assert.doesNotMatch(publicManifest, /localhost/)
})

test("Word installers validate the public add-in identity", () => {
  assert.match(macInstaller, new RegExp(addinId(publicManifest), "i"))
  assert.match(windowsInstaller, new RegExp(addinId(publicManifest), "i"))
})

test("install page, manifest route and mac installer agree on the public origin", () => {
  const installPage = readFileSync(new URL("../../word_addin/install/index.html", import.meta.url), "utf8")
  assert.match(installPage, /https:\/\/cciteheck-api\.onrender\.com\/manifest\.xml/)
  assert.match(installPage, /\/install\/install-ccitecheck\.command/)
  assert.match(macInstaller, /https:\/\/cciteheck-api\.onrender\.com\/manifest\.xml/)
})
