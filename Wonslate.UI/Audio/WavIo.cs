// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.IO;

namespace Wonslate.Audio;

/// <summary>
/// Minimal 16-bit PCM WAV reader/writer.
///
/// The pipeline works on raw float samples, so the file format has to be handled
/// somewhere; doing it here keeps it in one place and dependency-free (no NAudio just
/// to open a file). Chunks other than fmt/data are skipped, which is what the
/// sherpa-onnx sample wavs carry.
/// </summary>
public static class WavIo
{
    /// <summary>Read a PCM WAV into mono float samples (-1..1) plus its sample rate.</summary>
    public static (float[] Samples, int SampleRate) Read(string path)
    {
        using var fs = File.OpenRead(path);
        using var br = new BinaryReader(fs);

        if (new string(br.ReadChars(4)) != "RIFF") throw new InvalidDataException("not a RIFF file: " + path);
        br.ReadInt32();
        if (new string(br.ReadChars(4)) != "WAVE") throw new InvalidDataException("not a WAVE file: " + path);

        short format = -1, channels = 1, bits = -1;
        int rate = -1;
        var pcm = new List<float>();

        while (fs.Position + 8 <= fs.Length)
        {
            var id = new string(br.ReadChars(4));
            int size = br.ReadInt32();
            long start = fs.Position;

            if (id == "fmt ")
            {
                format = br.ReadInt16();
                channels = br.ReadInt16();
                rate = br.ReadInt32();
                br.ReadInt32();                 // byte rate
                br.ReadInt16();                 // block align
                bits = br.ReadInt16();
            }
            else if (id == "data")
            {
                var bytes = br.ReadBytes(size);
                if (format == 1 && bits == 16)
                {
                    for (int i = 0; i + 1 < bytes.Length; i += 2)
                    {
                        float v = BitConverter.ToInt16(bytes, i) / 32768.0f;
                        pcm.Add(v);
                    }
                }
                else if (format == 3 && bits == 32)
                {
                    for (int i = 0; i + 3 < bytes.Length; i += 4)
                        pcm.Add(BitConverter.ToSingle(bytes, i));
                }
                else
                {
                    throw new NotSupportedException($"unsupported wav format={format} bits={bits}");
                }
            }

            fs.Seek(start + size + (size & 1), SeekOrigin.Begin);
        }

        if (pcm.Count == 0) throw new InvalidDataException("no PCM samples in " + path);
        if (channels > 1) throw new NotSupportedException($"expected mono, got {channels} channels");

        return (pcm.ToArray(), rate);
    }

    /// <summary>Write mono float samples as a 16-bit PCM WAV (what System.Media.SoundPlayer needs).</summary>
    public static void Write(string path, float[] samples, int sampleRate)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        using var fs = File.Create(path);
        using var bw = new BinaryWriter(fs);

        int dataBytes = samples.Length * 2;
        bw.Write("RIFF".ToCharArray());
        bw.Write(36 + dataBytes);
        bw.Write("WAVE".ToCharArray());

        bw.Write("fmt ".ToCharArray());
        bw.Write(16);                       // PCM fmt chunk size
        bw.Write((short)1);                 // PCM
        bw.Write((short)1);                 // mono
        bw.Write(sampleRate);
        bw.Write(sampleRate * 2);           // byte rate
        bw.Write((short)2);                 // block align
        bw.Write((short)16);                // bits per sample

        bw.Write("data".ToCharArray());
        bw.Write(dataBytes);
        foreach (var s in samples)
        {
            var clamped = Math.Clamp(s, -1.0f, 1.0f);
            bw.Write((short)(clamped * 32767));
        }
    }
}
