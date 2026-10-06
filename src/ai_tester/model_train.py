from pyspark import SparkContext, SparkConf, SQLContext
from pyspark.sql import functions as F
from pyspark.sql.window import Window

conf = SparkConf().setMaster("local").setAppName("My App")
sc = SparkContext(conf=conf)
sqlContext = SQLContext(sc)

# 定义用户数据
dicts = [
    ['frank', '男', 16, '程序员', 3600, 1.0],
    ['alex', '女', 26, '项目经理', 3000, 1.0],
    ['frank', '男', 16, '程序员', 2600, 0.0],
    ['asdf', '男', 16, '程序员', 2600, 0.0],
    ['fragfsnk', '男', 16, '程序员', 2600, 0.0],
    ['frasdfgnk', '男', 16, '程序员', 2600, 0.0],
    ['frsdfgank', '男', 16, '程序员', 2600, 0.0],
    ['frsdfgdfank', '男', 16, '程序员', 2600, 0.0],
    ['frsdfgdfankdsaf', '男', 16, '程序员', 2600, 0.0],
    ['frsdfgdfank342', '男', 16, '程序员', 2600, 0.0],
    ['frsdfgdfank445', '男', 16, '程序员', 2600, 0.0],
    ['frsdfgdfank756', '男', 16, '程序员', 3600, 1.0],
    ['hdfg', '男', 16, '程序员', 2600, 0.0],
    ['frsdfncvgdfank', '男', 16, '程序员', 2600, 0.0],
    ['wert', '男', 16, '程序员', 2600, 0.0],
    ['sdfg', '男', 16, '程序员', 2600, 0.0],
    ['frssdffgdfank', '男', 16, '程序员', 2600, 0.0],
    ['asdf', '男', 16, '程序员', 2600, 0.0],
    ['zxcv', '男', 16, '程序员', 2600, 0.0],
    ['frsdfgdfank', '男', 16, '程序员', 2600, 0.0],
    ['vzxcv', '男', 16, '程序员', 2600, 0.0],
    ['zxcv', '男', 16, '程序员', 3600, 1.0],
    ['frsdfgdcvfank', '男', 16, '程序员', 3600, 1.0],
    ['frsdfgdcvfankasdf', '男', 16, '程序员', 3600, 1.0],
    ['asfghffgh', '男', 16, '程序员', 3600, 1.0],
    ['dfgh', '男', 16, '程序员', 3600, 1.0],
    ['frsdfgdcvbnmvbvfank', '男', 16, '程序员', 3600, 1.0],
    ['v', '男', 16, '程序员', 3600, 1.0],
    ['dasdfsadf', '男', 16, '程序员', 3600, 1.0],
    ['gghg', '男', 16, '程序员', 3600, 1.0],
]
rdd = sc.parallelize(dicts, 3)
# 假设用户数据有名字，性别，年龄，职位，本次交易额，以及最后的label（代表是否是欺诈行为，1.0为欺诈，0.0相反）
dataf = sqlContext.createDataFrame(rdd, ['name', 'gender', 'age', 'title', 'price', 'label'])


# 通过spark定义一段窗口，计算出用户一段时间内的最大消费额
windowSpec = Window.partitionBy(dataf.gender)
windowSpec = windowSpec.orderBy(dataf.age)
windowSpec = windowSpec.rowsBetween(Window.unboundedPreceding, Window.currentRow)
dataf.withColumn('max_price', F.max(dataf.price).over(windowSpec)).show()

from pyspark.ml.feature import StringIndexer, OneHotEncoder, VectorAssembler
from pyspark.ml.evaluation import BinaryClassificationEvaluator
from pyspark.ml.tuning import ParamGridBuilder, TrainValidationSplit

# 将非数值类型的字段转换为数值类型
stringIndexer = StringIndexer(inputCol="title", outputCol="title_num")
data_indexed = stringIndexer.fit(dataf).transform(dataf)
# 将类别特征进行独热编码，这是为了把字符串类型的离散特征，转换成计算机可以识别的数字
encoder = OneHotEncoder(inputCol="title_num", outputCol="title_onehot")
data_encoded = encoder.fit(data_indexed).transform(data_indexed)

# 将所有特征组合成一个特征向量
vectorAssembler = VectorAssembler(inputCols=["age", "title_onehot", "price"], outputCol="feature")
data_vector = vectorAssembler.transform(data_encoded)

# 将数据划分为训练集和验证集，一部分数据用来训练，而一部分数据用来测试模型的效果
(train_data, test_data) = data_vector.randomSplit([0.8, 0.2], seed=1234)

from pyspark.ml.classification import LogisticRegression

# 创建Logistic回归模型，这是最典型的机器学习算法
lr = LogisticRegression(labelCol="label", featuresCol="feature")

# 定义模型训练要用的参数
paramGrid = ParamGridBuilder().addGrid(lr.regParam, [0.01, 0.1, 1]).addGrid(lr.elasticNetParam, [0.0, 0.5, 1.0]).build()

# 定义评估指标
evaluator = BinaryClassificationEvaluator(labelCol="label", metricName="areaUnderROC")

# 使用训练集和验证进行模型调参和交叉验证
tvs = TrainValidationSplit(estimator=lr, estimatorParamMaps=paramGrid, evaluator=evaluator, trainRatio=0.8)
tvsModel = tvs.fit(train_data)

# 输出最佳参数（特征）组合
print("最佳参数组合: " + str(tvsModel.getEstimatorParamMaps()))

# 使用最佳参数组合训练模型
bestModel = tvsModel.bestModel

# 在测试集上评估模型效果，predictions中保存了每一条数据的预测结果
predictions = bestModel.transform(test_data)
result = evaluator.evaluate(predictions)
predictions.show()
print(result)

# 把模型保存到本地目录
tvsModel.write().overwrite().save('./model')
