package org.androidrescue.helper;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Base64;
import android.view.View;
import android.view.WindowManager;
import android.widget.*;
import org.json.JSONObject;
import java.io.*;
import java.text.SimpleDateFormat;
import java.util.*;

public final class MainActivity extends Activity {
    private final String[] permissions = {
        Manifest.permission.READ_CONTACTS, Manifest.permission.READ_SMS,
        Manifest.permission.READ_CALL_LOG, Manifest.permission.READ_CALENDAR,
        Manifest.permission.WRITE_EXTERNAL_STORAGE
    };
    private final String[] permissionLabels = {"Contacts", "Text messages / MMS", "Call history", "Calendars", "Export folder storage"};
    private TextView permissionStatus;
    private TextView status;
    private Button grant;
    private Button export;
    private ProgressBar progressBar;
    private boolean busy;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        if (Build.VERSION.SDK_INT >= 21) getWindow().setStatusBarColor(Color.rgb(17, 58, 73));
        ScrollView scroll = new ScrollView(this);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(24);
        layout.setPadding(pad, pad, pad, pad);
        scroll.addView(layout);
        TextView heading = text("Android Bay Helper", 27, Color.rgb(17, 58, 73));
        layout.addView(heading);
        layout.addView(text("Export your phone's local records over USB", 18, Color.rgb(44, 77, 88)));
        layout.addView(text("No Google account or internet is needed. This helper reads contacts, SMS/MMS, call history and calendars, then creates a new folder for the PC app to copy.", 16, Color.DKGRAY));
        layout.addView(text("1  Allow access", 20, Color.rgb(17, 58, 73)));
        permissionStatus = text("", 15, Color.DKGRAY);
        layout.addView(permissionStatus);
        grant = new Button(this);
        grant.setText("Grant permissions");
        grant.setOnClickListener(new View.OnClickListener() { public void onClick(View view) { requestAccess(); }});
        layout.addView(grant);
        Button settings = new Button(this);
        settings.setText("Open app permission settings");
        settings.setOnClickListener(new View.OnClickListener() {
            public void onClick(View view) {
                if (!busy) startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())));
            }
        });
        layout.addView(settings);
        layout.addView(text("2  Create the export", 20, Color.rgb(17, 58, 73)));
        layout.addView(text("Keep this screen open and the phone connected until finished. Missing permissions and provider restrictions appear in the report. Each run creates its own folder; existing exports stay in place.", 16, Color.DKGRAY));
        export = new Button(this);
        export.setText("Create export");
        export.setOnClickListener(new View.OnClickListener() { public void onClick(View view) { startExport(); }});
        layout.addView(export);
        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setIndeterminate(true);
        progressBar.setVisibility(View.GONE);
        layout.addView(progressBar);
        status = text("Ready. After export finishes, keep Phone data exports selected in the PC app and click Start transfer.", 16, Color.rgb(17, 58, 73));
        status.setTextIsSelectable(true);
        layout.addView(status);
        layout.addView(text("Scope: the current Android user/profile and records exposed by its system providers. Private app databases, passwords, tokens, cloud-only content, RCS chats and deleted records are outside this helper's access. Export files contain personal information; keep them in a trusted location.", 14, Color.DKGRAY));
        setContentView(scroll);
        refreshPermissions();
    }

    private int dp(int value) { return (int) (value * getResources().getDisplayMetrics().density + .5f); }
    private TextView text(String value, int size, int color) {
        TextView label = new TextView(this);
        label.setText(value);
        label.setTextSize(size);
        label.setTextColor(color);
        label.setPadding(0, dp(10), 0, dp(10));
        return label;
    }
    private boolean allowed(String permission) { return Build.VERSION.SDK_INT < 23 || checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED; }
    private void refreshPermissions() {
        if (permissionStatus == null) return;
        StringBuilder message = new StringBuilder();
        for (int i = 0; i < permissions.length; i++) message.append(allowed(permissions[i]) ? "Allowed: " : "Needs permission: ").append(permissionLabels[i]).append('\n');
        permissionStatus.setText(message.toString().trim());
    }
    private void requestAccess() {
        if (busy) return;
        if (Build.VERSION.SDK_INT >= 23) {
            ArrayList<String> needed = new ArrayList<String>();
            for (String permission : permissions) if (!allowed(permission)) needed.add(permission);
            if (!needed.isEmpty()) requestPermissions(needed.toArray(new String[needed.size()]), 100);
            else status.setText("Permissions allowed. Tap Create export.");
        } else status.setText("On this Android version, permissions were granted during installation. Tap Create export.");
        refreshPermissions();
    }
    @Override public void onRequestPermissionsResult(int requestCode, String[] requested, int[] results) {
        super.onRequestPermissionsResult(requestCode, requested, results);
        refreshPermissions();
        status.setText("Permission choices saved. You can create an export now. Denied categories will be recorded as errors. If SMS or call access stays unavailable, reinstall through the PC app and check app permission settings.");
    }
    @Override public void onResume() { super.onResume(); refreshPermissions(); }
    @Override public void onBackPressed() {
        if (busy) Toast.makeText(this, "Export is running. Keep this screen open until it finishes.", Toast.LENGTH_LONG).show();
        else super.onBackPressed();
    }
    private void startExport() {
        if (busy) return;
        if (!allowed(Manifest.permission.WRITE_EXTERNAL_STORAGE)) {
            status.setText("Allow export folder storage first: tap Grant permissions, then allow storage access.");
            return;
        }
        busy = true;
        grant.setEnabled(false);
        export.setEnabled(false);
        progressBar.setVisibility(View.VISIBLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        status.setText("Starting export…");
        new Thread(new Runnable() {
            public void run() {
                PowerManager.WakeLock wake = null;
                String finalMessage;
                try {
                    PowerManager manager = (PowerManager) getSystemService(POWER_SERVICE);
                    wake = manager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AndroidRescue:Export");
                    wake.acquire(2 * 60 * 60 * 1000L);
                    SimpleDateFormat format = new SimpleDateFormat("yyyyMMdd'T'HHmmss'Z'", Locale.US);
                    format.setTimeZone(TimeZone.getTimeZone("UTC"));
                    String run = format.format(new Date()) + "-" + UUID.randomUUID().toString().substring(0, 8);
                    final File directory = new File(Environment.getExternalStorageDirectory(), "AndroidRescue/exports/" + run);
                    JSONObject device = new JSONObject().put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL).put("androidVersion", Build.VERSION.RELEASE).put("sdk", Build.VERSION.SDK_INT);
                    JSONObject access = new JSONObject();
                    for (String permission : permissions) access.put(permission, allowed(permission));
                    device.put("permissionsAtStart", access);
                    ExportEngine engine = new ExportEngine(new ExportEngine.Provider() {
                        public Cursor query(String uri) { return getContentResolver().query(Uri.parse(uri), null, null, null, null); }
                        public InputStream open(String uri) throws Exception { return getContentResolver().openInputStream(Uri.parse(uri)); }
                        public String base64(byte[] value) { return Base64.encodeToString(value, Base64.NO_WRAP); }
                    }, new ExportEngine.Progress() {
                        public void update(final String message) { runOnUiThread(new Runnable() { public void run() { status.setText(message); }}); }
                    }, directory, device);
                    JSONObject result = engine.run();
                    int errorCount = result.getJSONArray("errors").length();
                    finalMessage = "Export finished" + (errorCount == 0 ? "." : " with " + errorCount + " reported issues.") + "\n\n" + result.getLong("exportedRows") + " provider records saved.\n\n" + directory.getAbsolutePath() + "\n\nOn the PC, keep Phone data exports selected and click Start transfer. Review report.json for each category. A finished export does not mean private app or cloud-only data was accessible.";
                } catch (Exception error) {
                    finalMessage = "Export did not finish: " + error.getClass().getSimpleName() + ": " + error.getMessage() + "\n\nCheck storage permission and free space, then retry. Any partial run remains in AndroidRescue/exports. Only report.json marks a finished attempt.";
                } finally {
                    if (wake != null && wake.isHeld()) wake.release();
                }
                final String message = finalMessage;
                runOnUiThread(new Runnable() {
                    public void run() {
                        status.setText(message);
                        busy = false;
                        grant.setEnabled(true);
                        export.setEnabled(true);
                        progressBar.setVisibility(View.GONE);
                        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                    }
                });
            }
        }, "AndroidRescueExport").start();
    }
}

