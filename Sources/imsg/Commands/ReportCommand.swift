import Commander
import Foundation
import IMsgCore

enum ReportCommand {
  static let spec = CommandSpec(
    name: "report",
    abstract: "Bulk export messages across chats for a date window",
    discussion: nil,
    signature: CommandSignatures.withRuntimeFlags(
      CommandSignature(
        options: CommandSignatures.baseOptions() + [
          .make(label: "limit", names: [.long("limit")], help: "Maximum messages to export"),
          .make(
            label: "direction", names: [.long("direction")],
            help: "sent, received, or both"),
          .make(
            label: "participants", names: [.long("participants")],
            help: "filter by participant handles", parsing: .upToNextOption),
          .make(label: "start", names: [.long("start")], help: "ISO8601 start (inclusive)"),
          .make(label: "end", names: [.long("end")], help: "ISO8601 end (exclusive)"),
        ],
        flags: [
          .make(
            label: "includeReactions", names: [.long("include-reactions")],
            help: "include tapback metadata for exported messages"
          )
        ]
      )
    ),
    usageExamples: [
      "imsg report --direction sent --start 2026-05-05T00:00:00Z --end 2026-05-06T00:00:00Z --json",
      "imsg report --direction both --start 2026-05-05T00:00:00Z --limit 500 --json",
    ]
  ) { values, runtime in
    try await run(values: values, runtime: runtime)
  }

  static func run(
    values: ParsedValues,
    runtime: RuntimeOptions,
    contactResolverFactory: @escaping () async -> any ContactResolving = {
      await ContactResolver.create()
    }
  ) async throws {
    if values.option("start") == nil && values.option("end") == nil {
      throw ParsedValuesError.missingOption("start or end")
    }

    let dbPath = values.option("db") ?? MessageStore.defaultPath
    let limit = values.optionInt("limit") ?? 1_000
    let participants = values.optionValues("participants")
      .flatMap { $0.split(separator: ",").map { String($0) } }
      .filter { !$0.isEmpty }
    let filter = try MessageFilter.fromISO(
      participants: participants,
      startISO: values.option("start"),
      endISO: values.option("end")
    )
    let isFromMe = try directionFilter(values.option("direction") ?? "sent")
    let includeReactions = values.flag("includeReactions")

    let store = try MessageStore(path: dbPath)
    let messages = try store.messages(
      limit: limit,
      filter: filter,
      isFromMe: isFromMe,
      includeReactions: includeReactions
    )
    let contacts = await contactResolverFactory()

    if runtime.jsonOutput {
      let cache = ChatCache(store: store)
      let reactionsByMessageID = includeReactions ? try store.reactions(for: messages) : [:]
      for message in messages {
        let payload = try await buildMessagePayload(
          store: store,
          cache: cache,
          message: message,
          includeAttachments: false,
          includeReactions: includeReactions,
          prefetchedReactions: reactionsByMessageID[message.rowID] ?? [],
          contactResolver: contacts
        )
        try JSONLines.printObject(payload)
      }
      return
    }

    for message in messages {
      let direction = message.isFromMe ? "sent" : "recv"
      let timestamp = CLIISO8601.format(message.date)
      let sender =
        message.isFromMe
        ? "me" : (contacts.displayName(for: message.sender) ?? message.sender)
      StdoutWriter.writeLine("[\(message.chatID)] \(timestamp) [\(direction)] \(sender): \(message.text)")
    }
  }

  private static func directionFilter(_ raw: String) throws -> Bool? {
    switch raw.lowercased() {
    case "sent", "me", "outgoing":
      return true
    case "received", "recv", "incoming":
      return false
    case "both", "all":
      return nil
    default:
      throw ParsedValuesError.invalidOption("direction")
    }
  }
}
