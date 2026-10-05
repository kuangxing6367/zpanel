import Instances from '../parts/Instances'

/** 运行时：当前节点作用域下的实例托管。
 *  实例是「节点上的实体」—— 点击行进入实例详情（/instances/:id），
 *  或者从「节点」页点进某台机器看它自己的实例。 */
export default function Runtime() {
  return <Instances />
}
