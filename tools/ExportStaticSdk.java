/* Supplemental ordinary JADX export. These files are not REA Evidence.
 * Loads the supplied APK as data only, never executes its application code.
 */
import jadx.api.JadxArgs;
import jadx.api.JadxDecompiler;
import jadx.api.JavaClass;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

public class ExportStaticSdk {
    public static void main(String[] arguments) throws Exception {
        JadxArgs settings = new JadxArgs();
        settings.setInputFile(new File(arguments[0]));
        settings.setSkipResources(true);
        settings.setThreadsCount(1);
        settings.setInlineAnonymousClasses(false);
        settings.setInlineMethods(false);
        settings.setAllowInlineKotlinLambda(false);
        Path output = Path.of(arguments[1]).toAbsolutePath();
        Files.createDirectories(output);
        try (JadxDecompiler decompiler = new JadxDecompiler(settings)) {
            decompiler.load();
            List<String> index = new ArrayList<>();
            int count = 0;
            for (JavaClass item : decompiler.getClasses()) {
                String name = item.getRawName();
                if (name.startsWith("com.gizwits.smart.sdk")) index.add(name);
                boolean selected = false;
                for (int i = 2; i < arguments.length; i++) {
                    if (name.startsWith(arguments[i])) selected = true;
                }
                if (!selected) continue;
                Path target = output.resolve(name.replace('.', '/') + ".java");
                Files.createDirectories(target.getParent());
                Files.writeString(target, item.getCode(), StandardCharsets.UTF_8);
                count++;
            }
            Files.write(output.resolve("sdk-class-index.txt"), index, StandardCharsets.UTF_8);
            System.out.println("Exported " + count + " selected static classes");
        }
    }
}
