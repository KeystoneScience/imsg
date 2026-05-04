import Testing

@testable import IMsgCore

@Test
func noOpContactResolverReturnsNoMatches() {
  let resolver = NoOpContactResolver()
  #expect(resolver.contactsUnavailable == false)
  #expect(resolver.displayName(for: "+15551234567") == nil)
  #expect(resolver.displayNames(for: ["+15551234567"]).isEmpty)
  #expect(resolver.searchByName("John").isEmpty)
}

@Test
func noOpContactResolverCanRepresentUnavailableContacts() {
  let resolver = NoOpContactResolver(contactsUnavailable: true)
  #expect(resolver.contactsUnavailable == true)
}
