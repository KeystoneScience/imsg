import Foundation
import SQLite
import Testing

@testable import IMsgCore

private struct WatcherTestStore {
  let store: MessageStore
  let insertMessage: (Int64, String) throws -> Void
}

private enum WatcherTestDatabase {
  static func appleEpoch(_ date: Date) -> Int64 {
    let seconds = date.timeIntervalSince1970 - MessageStore.appleEpochOffset
    return Int64(seconds * 1_000_000_000)
  }

  static func makeStore() throws -> MessageStore {
    let db = try Connection(.inMemory)
    try db.execute(
      """
      CREATE TABLE message (
        ROWID INTEGER PRIMARY KEY,
        handle_id INTEGER,
        text TEXT,
        date INTEGER,
        is_from_me INTEGER,
        service TEXT
      );
      """
    )
    try db.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);")
    try db.execute("CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);")
    try db.execute(
      "CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);")

    let now = Date()
    try db.run("INSERT INTO handle(ROWID, id) VALUES (1, '+123')")
    try db.run(
      """
      INSERT INTO message(ROWID, handle_id, text, date, is_from_me, service)
      VALUES (1, 1, 'hello', ?, 0, 'iMessage')
      """,
      appleEpoch(now)
    )
    try db.run("INSERT INTO chat_message_join(chat_id, message_id) VALUES (1, 1)")

    return try MessageStore(
      connection: db, path: ":memory:", hasAttributedBody: false, hasReactionColumns: false)
  }

  static func makeMutableStore() throws -> WatcherTestStore {
    let db = try Connection(.inMemory)
    try db.execute(
      """
      CREATE TABLE message (
        ROWID INTEGER PRIMARY KEY,
        handle_id INTEGER,
        text TEXT,
        date INTEGER,
        is_from_me INTEGER,
        service TEXT
      );
      """
    )
    try db.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);")
    try db.execute("CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);")
    try db.execute(
      "CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);")
    try db.run("INSERT INTO handle(ROWID, id) VALUES (1, '+123')")

    let store = try MessageStore(
      connection: db, path: ":memory:", hasAttributedBody: false, hasReactionColumns: false)
    return WatcherTestStore(
      store: store,
      insertMessage: { rowID, text in
        try store.withConnection { db in
          try db.run(
            """
            INSERT INTO message(ROWID, handle_id, text, date, is_from_me, service)
            VALUES (?, 1, ?, ?, 0, 'iMessage')
            """,
            rowID,
            text,
            appleEpoch(Date())
          )
          try db.run("INSERT INTO chat_message_join(chat_id, message_id) VALUES (1, ?)", rowID)
        }
      }
    )
  }
}

@Test
func messageWatcherYieldsExistingMessages() async throws {
  let store = try WatcherTestDatabase.makeStore()
  let watcher = MessageWatcher(store: store)
  let stream = watcher.stream(
    chatID: nil,
    sinceRowID: -1,
    configuration: MessageWatcherConfiguration(debounceInterval: 0.01, batchLimit: 10)
  )

  let task = Task { () throws -> Message? in
    var iterator = stream.makeAsyncIterator()
    return try await iterator.next()
  }

  let message = try await task.value
  #expect(message?.text == "hello")
}

@Test
func messageWatcherFallbackPollYieldsMessagesWithoutFileEvents() async throws {
  let fixture = try WatcherTestDatabase.makeMutableStore()
  let watcher = MessageWatcher(store: fixture.store)
  let stream = watcher.stream(
    chatID: nil,
    sinceRowID: 0,
    configuration: MessageWatcherConfiguration(
      debounceInterval: 60,
      fallbackPollInterval: 0.01,
      batchLimit: 10
    )
  )

  let task = Task { () throws -> Message? in
    var iterator = stream.makeAsyncIterator()
    return try await iterator.next()
  }

  try await Task.sleep(nanoseconds: 20_000_000)
  try fixture.insertMessage(2, "fallback")

  let message = try await task.value
  #expect(message?.rowID == 2)
  #expect(message?.text == "fallback")
}
