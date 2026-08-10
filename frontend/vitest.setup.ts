import "@testing-library/jest-dom/vitest";

// Node 26's built-in `localStorage` global shadows jsdom's before it is
// configured with `--localstorage-file`, leaving `window.localStorage`
// undefined inside Vitest's jsdom environment. Replace it with a plain
// in-memory Storage so `frontend/src/lib/api.ts`'s tokenStore has
// somewhere real to read and write.
class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length() {
    return this.store.size;
  }
  clear() {
    this.store.clear();
  }
  getItem(key: string) {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  key(index: number) {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string) {
    this.store.delete(key);
  }
  setItem(key: string, value: string) {
    this.store.set(key, String(value));
  }
}

const memoryStorage = new MemoryStorage();
Object.defineProperty(window, "localStorage", { value: memoryStorage, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: memoryStorage, configurable: true });
