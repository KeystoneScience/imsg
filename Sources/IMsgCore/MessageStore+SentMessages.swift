import Foundation
import SQLite

extension MessageStore {
  public func chatInfo(matchingTarget target: String) throws -> ChatInfo? {
    let candidates = Self.chatTargetCandidates(target)
    guard !candidates.isEmpty else { return nil }

    let placeholders = Array(repeating: "?", count: candidates.count).joined(separator: ",")
    let sql = """
      SELECT c.ROWID, IFNULL(c.chat_identifier, '') AS identifier, IFNULL(c.guid, '') AS guid,
             IFNULL(c.display_name, c.chat_identifier) AS name, IFNULL(c.service_name, '') AS service
      FROM chat c
      WHERE c.chat_identifier IN (\(placeholders))
         OR c.guid IN (\(placeholders))
      LIMIT 1
      """
    let bindings: [Binding?] = candidates + candidates
    return try withConnection { db in
      guard let row = try db.prepare(sql, bindings).makeIterator().next() else { return nil }
      return ChatInfo(
        id: int64Value(row[0]) ?? 0,
        identifier: stringValue(row[1]),
        guid: stringValue(row[2]),
        name: stringValue(row[3]),
        service: stringValue(row[4])
      )
    }
  }

  public func latestUnjoinedSentMessageRowID(
    matchingTargetHandles handles: [String],
    since date: Date
  ) throws -> Int64? {
    let candidates = Self.chatTargetHandleCandidates(handles)
    guard !candidates.isEmpty else { return nil }

    let placeholders = Array(repeating: "?", count: candidates.count).joined(separator: ",")
    let sql = """
      SELECT m.ROWID
      FROM message m
      LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
      LEFT JOIN handle h ON h.ROWID = m.handle_id
      WHERE m.is_from_me = 1
        AND m.date >= ?
        AND IFNULL(m.text, '') = ''
        AND cmj.message_id IS NULL
        AND IFNULL(h.id, '') IN (\(placeholders))
      ORDER BY m.date DESC, m.ROWID DESC
      LIMIT 1
      """
    let bindings: [Binding?] = [MessageStore.appleEpoch(date)] + candidates
    return try withConnection { db in
      guard let row = try db.prepare(sql, bindings).makeIterator().next() else { return nil }
      return int64Value(row[0])
    }
  }

  private static func chatTargetHandleCandidates(_ handles: [String]) -> [String] {
    var candidates: [String] = []
    for handle in handles {
      candidates.append(contentsOf: chatTargetCandidates(handle))
    }
    return dedupe(candidates)
  }

  private static func chatTargetCandidates(_ target: String) -> [String] {
    let trimmed = target.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return [] }

    var candidates = [trimmed]
    if let toggled = toggledAnyGroupPolarity(trimmed) {
      candidates.append(toggled)
    }
    if let bare = bareAnyGroupIdentifier(trimmed) {
      candidates.append(bare)
    }
    return dedupe(candidates)
  }

  private static func toggledAnyGroupPolarity(_ value: String) -> String? {
    if value.hasPrefix("any;+;") {
      return "any;-;" + value.dropFirst("any;+;".count)
    }
    if value.hasPrefix("any;-;") {
      return "any;+;" + value.dropFirst("any;-;".count)
    }
    return nil
  }

  private static func bareAnyGroupIdentifier(_ value: String) -> String? {
    if value.hasPrefix("any;+;") {
      return String(value.dropFirst("any;+;".count))
    }
    if value.hasPrefix("any;-;") {
      return String(value.dropFirst("any;-;".count))
    }
    return nil
  }

  private static func dedupe(_ values: [String]) -> [String] {
    var seen = Set<String>()
    var result: [String] = []
    for value in values where !value.isEmpty {
      if seen.insert(value).inserted {
        result.append(value)
      }
    }
    return result
  }
}
