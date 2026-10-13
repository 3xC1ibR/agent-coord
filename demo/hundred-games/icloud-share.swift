// Publish an iCloud Drive file and print its share URL as JSON.
// usage: swift demo/orchestration/icloud-share.swift "/path/in/iCloud Drive/file.mp4"
import Foundation
let path = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
let url = URL(fileURLWithPath: path)
var expiration: NSDate? = nil
do {
  let keys: Set<URLResourceKey> = [.isUbiquitousItemKey, .ubiquitousItemIsUploadedKey, .ubiquitousItemIsUploadingKey]
  let values = try url.resourceValues(forKeys: keys)
  let shared = try FileManager.default.url(forPublishingUbiquitousItemAt: url, expiration: &expiration)
  let formatter = ISO8601DateFormatter()
  let out: [String: Any] = ["url": shared.absoluteString, "expiration": expiration.map { formatter.string(from: $0 as Date) } ?? NSNull(),
    "is_ubiquitous": values.isUbiquitousItem ?? false, "is_uploaded": values.ubiquitousItemIsUploaded ?? false, "is_uploading": values.ubiquitousItemIsUploading ?? false]
  let data = try JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
  print(String(data: data, encoding: .utf8)!)
} catch {
  FileHandle.standardError.write(("error: " + String(describing: error) + "\n").data(using: .utf8)!)
  exit(1)
}
