import Foundation
import IMsgCore

enum SentMessageVerifier {
  static func resolveSentMessage(
    store: MessageStore,
    options: MessageSendOptions,
    chatID: Int64?,
    sentAt: Date
  ) async throws -> Message? {
    guard !options.text.isEmpty else { return nil }

    let lowerBound = sentAt.addingTimeInterval(-2)
    let deadline = Date().addingTimeInterval(2)
    repeat {
      if Task.isCancelled { return nil }
      if let message = try store.latestSentMessage(
        matchingText: options.text,
        chatID: chatID,
        since: lowerBound
      ) {
        return message
      }
      try await Task.sleep(nanoseconds: 100_000_000)
    } while Date() < deadline
    return nil
  }

  static func throwIfMisroutedChatSend(
    store: MessageStore,
    options: MessageSendOptions,
    sentAt: Date
  ) throws {
    let handles = [options.chatGUID, options.chatIdentifier].filter { !$0.isEmpty }
    guard !handles.isEmpty else { return }
    let lowerBound = sentAt.addingTimeInterval(-2)
    guard
      let rowID = try store.latestUnjoinedSentMessageRowID(
        matchingTargetHandles: handles,
        since: lowerBound
      )
    else {
      return
    }

    throw IMsgError.appleScriptFailure(
      "Messages accepted the chat send but wrote an unjoined empty outgoing row (\(rowID)); delivery to the target chat was not confirmed"
    )
  }
}
