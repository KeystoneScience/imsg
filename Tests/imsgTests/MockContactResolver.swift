import IMsgCore

final class MockContactResolver: ContactResolving, Sendable {
  let contactsUnavailable: Bool
  private let names: [String: String]
  private let matches: [ContactMatch]

  init(
    names: [String: String] = [:],
    matches: [ContactMatch] = [],
    contactsUnavailable: Bool = false
  ) {
    self.names = names
    self.matches = matches
    self.contactsUnavailable = contactsUnavailable
  }

  func displayName(for handle: String) -> String? {
    names[handle]
  }

  func displayNames(for handles: [String]) -> [String: String] {
    var resolved: [String: String] = [:]
    for handle in handles {
      if let name = displayName(for: handle) {
        resolved[handle] = name
      }
    }
    return resolved
  }

  func searchByName(_ query: String) -> [ContactMatch] {
    let normalizedQuery = query.lowercased()
    return matches.filter { $0.name.lowercased().contains(normalizedQuery) }
  }
}
