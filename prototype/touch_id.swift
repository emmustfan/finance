import CryptoKit
import Darwin
import Foundation
import LocalAuthentication
import Security

private let saltSize = 16
private let archiveKeySize = 32
private let maxSealedKeySize = 4096
private let purpose = Data("org.familyfinance.archive.touch-id.v1".utf8)

private enum Failure: Error {
    case unavailable, invalidInput, authenticationFailed, sealingFailed
}

private struct SealedArchiveKey: Codable {
    let enclaveKey: Data
    let ephemeralPublicKey: Data
    let encryptedArchiveKey: Data
}

private func touchContext(reason: String) throws -> LAContext {
    let context = LAContext()
    context.localizedFallbackTitle = ""
    context.localizedReason = reason
    guard SecureEnclave.isAvailable,
          context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil),
          context.biometryType == .touchID else {
        throw Failure.unavailable
    }
    return context
}

private func encryptionKey(shared: SharedSecret, salt: Data) -> SymmetricKey {
    shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: salt, sharedInfo: purpose, outputByteCount: 32)
}

private func enroll(salt: Data, archiveKey: Data) async throws -> Data {
    let reason = "Разрешите открывать семейный архив с Touch ID."
    let context = try touchContext(reason: reason)
    guard try await context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) else {
        throw Failure.authenticationFailed
    }
    guard let access = SecAccessControlCreateWithFlags(
        nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, [.privateKeyUsage, .biometryCurrentSet], nil
    ) else {
        throw Failure.sealingFailed
    }
    let enclaveKey = try SecureEnclave.P256.KeyAgreement.PrivateKey(accessControl: access, authenticationContext: context)
    let ephemeral = P256.KeyAgreement.PrivateKey()
    let shared = try ephemeral.sharedSecretFromKeyAgreement(with: enclaveKey.publicKey)
    let key = encryptionKey(shared: shared, salt: salt)
    guard let ciphertext = try AES.GCM.seal(archiveKey, using: key, authenticating: salt).combined else {
        throw Failure.sealingFailed
    }
    return try JSONEncoder().encode(SealedArchiveKey(
        enclaveKey: enclaveKey.dataRepresentation,
        ephemeralPublicKey: ephemeral.publicKey.x963Representation,
        encryptedArchiveKey: ciphertext
    ))
}

private func unlock(salt: Data, payload: Data) throws -> Data {
    let sealed = try JSONDecoder().decode(SealedArchiveKey.self, from: payload)
    let context = try touchContext(reason: "Откройте семейный архив с Touch ID.")
    let enclaveKey = try SecureEnclave.P256.KeyAgreement.PrivateKey(
        dataRepresentation: sealed.enclaveKey, authenticationContext: context
    )
    let ephemeral = try P256.KeyAgreement.PublicKey(x963Representation: sealed.ephemeralPublicKey)
    // The enclave checks the enrolled fingerprints when it uses the private key.
    let shared = try enclaveKey.sharedSecretFromKeyAgreement(with: ephemeral)
    let box = try AES.GCM.SealedBox(combined: sealed.encryptedArchiveKey)
    let archiveKey = try AES.GCM.open(box, using: encryptionKey(shared: shared, salt: salt), authenticating: salt)
    guard archiveKey.count == archiveKeySize else { throw Failure.invalidInput }
    return archiveKey
}

@main
struct FinanceTouchID {
    static func main() async {
        do {
            switch CommandLine.arguments.dropFirst().first {
            case "available":
                _ = try touchContext(reason: "Вход с Touch ID.")
            case "enroll", "unlock":
                let action = CommandLine.arguments[1]
                let input = FileHandle.standardInput.readData(ofLength: saltSize + maxSealedKeySize + 1)
                guard input.count > saltSize, input.count <= saltSize + maxSealedKeySize else {
                    throw Failure.invalidInput
                }
                let salt = input.prefix(saltSize)
                let payload = input.dropFirst(saltSize)
                if action == "enroll", payload.count == archiveKeySize {
                    FileHandle.standardOutput.write(try await enroll(salt: salt, archiveKey: payload))
                } else if action == "unlock" {
                    FileHandle.standardOutput.write(try unlock(salt: salt, payload: payload))
                } else {
                    throw Failure.invalidInput
                }
            default:
                throw Failure.invalidInput
            }
        } catch Failure.unavailable {
            exit(2)
        } catch {
            exit(1)
        }
    }
}
