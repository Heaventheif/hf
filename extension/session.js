export class SessionManager {
  constructor() { this.id = 1; }
  reset() { this.id += 1; return this.id; }
  isCurrent(id) { return id === this.id; }
}
