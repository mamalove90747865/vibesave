import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;

public class YouTubeDownloader {

    public static void main(String[] args) {
        if (args.length == 0) {
            System.out.println("Usage: java YouTubeDownloader <YouTube_URL> [output_folder]");
            System.out.println("Example: java YouTubeDownloader https://www.youtube.com/watch?v=dQw4w9WgXcQ");
            return;
        }

        String url = args[0];
        String outputFolder = (args.length > 1) ? args[1] : "."; // default to current directory

        // Build yt-dlp command (highly customizable)
        List<String> command = new ArrayList<>();
        command.add("yt-dlp");                    // or full path: "C:\\yt-dlp\\yt-dlp.exe"
        command.add("--output");                  // output template
        command.add(outputFolder + "/%(title)s [%(id)s].%(ext)s");
        command.add("--format");                  // best quality video + audio (merges with ffmpeg if available)
        command.add("bestvideo+bestaudio/best");
        command.add("--no-playlist");             // remove if you want playlists
        command.add("--restrict-filenames");      // safe filenames
        command.add("--no-warnings");
        command.add("--progress");                // show download progress
        command.add(url);

        try {
            ProcessBuilder processBuilder = new ProcessBuilder(command);
            processBuilder.redirectErrorStream(true); // merge stdout and stderr
            Process process = processBuilder.start();

            // Read and print yt-dlp output in real-time (progress, errors, etc.)
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream()))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    System.out.println(line);
                }
            }

            int exitCode = process.waitFor();
            if (exitCode == 0) {
                System.out.println("\n✅ Download completed successfully!");
            } else {
                System.out.println("\n❌ Download failed with exit code: " + exitCode);
            }
        } catch (IOException e) {
            System.err.println("Error starting yt-dlp: " + e.getMessage());
            System.err.println("Make sure yt-dlp is installed and in your PATH.");
        } catch (InterruptedException e) {
            System.err.println("Download interrupted.");
            Thread.currentThread().interrupt();
        }
    }
}