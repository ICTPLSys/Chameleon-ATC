import java.util.Arrays;

import org.apache.spark.SparkConf;
import org.apache.spark.api.java.JavaRDD;
import org.apache.spark.api.java.JavaSparkContext;
import org.apache.spark.api.java.function.Function;
import org.apache.spark.mllib.clustering.KMeans;
import org.apache.spark.mllib.clustering.KMeansModel;
import org.apache.spark.mllib.linalg.Vector;
import org.apache.spark.mllib.linalg.Vectors;

/**
 * Spark MLlib KMeans driver matching the JavaKMeansExample used by spark-test.
 *
 * Input lines contain a whitespace-separated dense vector.  The extra
 * partitions argument lets a small validation prefix still exercise all
 * Guest vCPUs; it does not change the KMeans data or algorithm.
 */
public final class ChameleonSparkKMeans {
    private ChameleonSparkKMeans() {}

    private static final class ParsePoint implements Function<String, Vector> {
        @Override
        public Vector call(String line) {
            String trimmed = line.trim();
            if (trimmed.isEmpty()) {
                throw new IllegalArgumentException("empty input record");
            }
            String[] fields = trimmed.split("\\s+");
            double[] values = new double[fields.length];
            for (int i = 0; i < fields.length; i++) {
                values[i] = Double.parseDouble(fields[i]);
            }
            return Vectors.dense(values);
        }
    }

    public static void main(String[] args) {
        if (args.length != 4 && args.length != 5) {
            System.err.println(
                "Usage: ChameleonSparkKMeans <input> <clusters> <iterations> <partitions> [seed=42]");
            System.exit(2);
        }

        String input = args[0];
        int clusters = Integer.parseInt(args[1]);
        int iterations = Integer.parseInt(args[2]);
        int partitions = Integer.parseInt(args[3]);
        long seed = args.length == 5 ? Long.parseLong(args[4]) : 42L;
        if (clusters <= 0 || iterations <= 0 || partitions <= 0) {
            throw new IllegalArgumentException("clusters, iterations and partitions must be positive");
        }

        SparkConf conf = new SparkConf().setAppName("ChameleonSparkKMeans");
        try (JavaSparkContext context = new JavaSparkContext(conf)) {
            JavaRDD<Vector> points = context.textFile(input, partitions)
                .map(new ParsePoint())
                .cache();

            System.out.println("KMeans initialization seed = " + seed);
            KMeansModel model = new KMeans().setK(clusters)
                .setMaxIterations(iterations).setSeed(seed).run(points.rdd());
            System.out.println("Cluster centers:");
            Arrays.stream(model.clusterCenters()).forEach(System.out::println);

            // spark-test computes the cost twice, causing two cached-data scans.
            double cost = model.computeCost(points.rdd());
            System.out.println("Cost: " + cost);
            double withinSetSse = model.computeCost(points.rdd());
            System.out.println("Within Set Sum of Squared Errors = " + withinSetSse);
        }
    }
}
